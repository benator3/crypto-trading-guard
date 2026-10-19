import os
import csv
from datetime import datetime
import requests
import ccxt
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

# 이제 os.getenv('EXCHANGE_API_KEY') 로 안전하게 읽어옵니다.
# [선택] Google Gemini API를 활용할 경우 주석 해제 (pip install google-genai)
# from google import genai

class TradingDisciplineAssistant:
    def __init__(self, dry_run=True):
        """
        원칙 강제형 코인 선물 트레이딩 어시스턴트 초기화
        :param dry_run: True일 경우 실제 거래소 주문을 넣지 않고 계산과 검증만 수행 (안전 모드)
        """
        self.dry_run = dry_run
        
        # 1. 불변 리스크 원칙 파라미터 (절대 수정 금지 구역)
        self.MAX_RISK_PER_TRADE = 0.01      # 1회 최대 허용 손실: 총원금의 1% (0.01)
        self.MIN_RR_RATIO = 2.0             # 최소 허용 손익비: 1 : 2.0 이상만 진입 허용
        self.MAX_DAILY_LOSS_PCT = 0.02      # 일일 최대 손실 한도: 원금의 -2% 도달 시 당일 매매 잠금
        self.MAX_CONSECUTIVE_LOSSES = 2     # 일일 최대 연속 손절 횟수: 2연패 시 당일 매매 잠금
        self.DEFAULT_LEVERAGE = 5           # 기본 격리 레버리지 배율 (수량 역산으로 리스크는 1%로 고정됨)
        self.JOURNAL_FILE = "trade_journal.csv"

        # 2. 거래소 API 설정 (환경변수 사용 권장, 바이낸스 선물 기준 예시)
        self.exchange = ccxt.binance({
            'apiKey': os.getenv('EXCHANGE_API_KEY', 'YOUR_API_KEY'),
            'secret': os.getenv('EXCHANGE_SECRET_KEY', 'YOUR_SECRET_KEY'),
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}  # 선물(Future) 지갑 기준
        })

        # 3. 텔레그램 알림 설정 (선택 사항)
        self.tg_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        self.tg_chat_id = os.getenv('TELEGRAM_CHAT_ID', '')

        self._init_journal()

    def _init_journal(self):
        """매매 일지 CSV 파일이 없으면 헤더를 생성합니다."""
        if not os.path.exists(self.JOURNAL_FILE):
            with open(self.JOURNAL_FILE, mode='w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow([
                    "일시", "종목", "포지션", "총원금(USDT)", "진입가", "손절가", "익절가",
                    "손절폭(%)", "손익비(R/R)", "1회허용손실($)", "계산된주문수량", "진입근거", "실행상태"
                ])

    def send_telegram_alert(self, message: str):
        """텔레그램으로 매매 검증 결과 및 주문 내역을 전송합니다."""
        if not self.tg_token or not self.tg_chat_id:
            return
        url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
        try:
            requests.post(url, json={"chat_id": self.tg_chat_id, "text": message}, timeout=5)
        except Exception as e:
            print(f"[경고] 텔레그램 전송 실패: {e}")

    # =========================================================================
    # 모듈 1: 일일 킬 스위치 (Daily Kill-Switch)
    # =========================================================================
    def check_kill_switch(self, today_Loss_count: int, today_pnl_pct: float) -> bool:
        """당일 연패 횟수 또는 일일 손실 한도 초과 여부를 검사합니다."""
        print("\n[모듈 1] 일일 킬 스위치(Kill-Switch) 작동 상태 점검 중...")
        if today_Loss_count >= self.MAX_CONSECUTIVE_LOSSES:
            print(f"🚨 [차단] 금일 {today_Loss_count}연패 발생! 원칙에 따라 오늘 자정까지 매매가 강제 잠금됩니다.")
            return False
        if today_pnl_pct <= -self.MAX_DAILY_LOSS_PCT:
            print(f"🚨 [차단] 금일 누적 손실률({today_pnl_pct*100:.2f}%)이 한도(-2.0%)에 도달했습니다. 매매를 종료합니다.")
            return False
        print("✅ 킬 스위치 통과: 정상 매매 가능 상태입니다.")
        return True

    # =========================================================================
    # 모듈 2: 리스크·손익비 수문장 및 포지션 수량 자동 역산기
    # =========================================================================
    def calculate_and_validate_order(self, balance: float, side: str, entry: float, stop_loss: float, take_profit: float):
        """
        진입가·손절가·익절가의 논리적 오류를 검증하고, 손익비 1:2 기준과 1% 리스크 수량을 계산합니다.
        """
        print("\n[모듈 2] 손익비 및 포지션 사이징 수학적 검증 중...")
        side = side.upper()
        if side not in ["LONG", "SHORT"]:
            raise ValueError("포지션 방향은 'LONG' 또는 'SHORT'만 입력 가능합니다.")

        # 1. 방향별 손절가·익절가 위치 논리 검증 (실수 방지)
        if side == "LONG" and not (stop_loss < entry < take_profit):
            raise ValueError("❌ [입력 오류] LONG 포지션은 반드시 (손절가 < 진입가 < 익절가) 순서여야 합니다.")
        if side == "SHORT" and not (take_profit < entry < stop_loss):
            raise ValueError("❌ [입력 오류] SHORT 포지션은 반드시 (익절가 < 진입가 < 손절가) 순서여야 합니다.")

        # 2. 1코인당 손실 폭 및 수익 폭 계산
        risk_per_unit = abs(entry - stop_loss)
        reward_per_unit = abs(take_profit - entry)
        stop_distance_pct = (risk_per_unit / entry) * 100

        # 너무 짧은 손절 폭(0.2% 미만)은 거래소 수수료와 노이즈에 즉시 털리므로 경고 차단
        if stop_distance_pct < 0.2:
            raise ValueError(f"❌ [원칙 위반] 손절 폭({stop_distance_pct:.2f}%)이 너무 좁습니다. 수수료와 호가 노이즈를 고려해 최소 0.2% 이상으로 설정하세요.")

        # 3. 손익비(Risk/Reward Ratio) 검증
        rr_ratio = reward_per_unit / risk_per_unit
        if rr_ratio < self.MIN_RR_RATIO:
            raise ValueError(
                f"🚨 [진입 거부] 계산된 손익비가 1 : {rr_ratio:.2f} 입니다. "
                f"최소 기준(1 : {self.MIN_RR_RATIO}) 미달이므로 주문을 물리적으로 차단합니다."
            )

        # 4. 1회 최대 허용 손실액(R) 기반 주문 수량(Quantity) 역산
        max_loss_usd = balance * self.MAX_RISK_PER_TRADE
        quantity = max_loss_usd / risk_per_unit
        position_notional_usd = quantity * entry

        # 5. 왕복 지정가 수수료(0.02% * 2 = 0.04%) 예상치 계산
        estimated_fee_usd = position_notional_usd * 0.0004

        result = {
            "side": side,
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "stop_distance_pct": round(stop_distance_pct, 3),
            "rr_ratio": round(rr_ratio, 2),
            "max_loss_usd": round(max_loss_usd, 2),
            "quantity": round(quantity, 4),
            "position_notional_usd": round(position_notional_usd, 2),
            "required_margin_usd": round(position_notional_usd / self.DEFAULT_LEVERAGE, 2),
            "estimated_fee_usd": round(estimated_fee_usd, 2)
        }

        print(f"✅ 수학적 검증 통과!")
        print(f"   ▶ 손절 폭: {result['stop_distance_pct']}% | 손익비: 1 : {result['rr_ratio']}")
        print(f"   ▶ 손절 시 최대 손실액: ${result['max_loss_usd']} (원금의 1.0% 고정, 예상 수수료 약 ${result['estimated_fee_usd']} 별도)")
        print(f"   ▶ 진입해야 할 총 포지션 규모: ${result['position_notional_usd']} (주문 수량: {result['quantity']} 개)")
        print(f"   ▶ 필요 증거금({self.DEFAULT_LEVERAGE}배 격리 기준): ${result['required_margin_usd']}")
        return result

    # =========================================================================
    # 모듈 3: AI 매매 감사관 (뇌동매매 및 진입 근거 필터링)
    # =========================================================================
    def audit_entry_reason(self, reason: str) -> bool:
        """
        사용자가 작성한 진입 근거를 검사하여 감정적 뇌동매매를 차단합니다.
        (Gemini API 키를 연결하면 LLM 심층 분석도 가능하며, 기본은 규칙 기반 + 키워드 필터로 즉시 작동합니다.)
        """
        print("\n[모듈 3] AI 매매 감사관의 진입 근거 점검 중...")
         banned_keywords = ["느낌", "감으로", "본전", "복구", "급등", "떡상", "일단", "가즈아", "심심해서"]
        
        if len(reason.strip()) < 15:
            print("🚨 [진입 거부] 진입 근거가 너무 짧습니다(15자 미만). 지지·저항 근거와 시간대(예: 4시간봉 추세선 지지 등)를 구체적으로 적으세요.")
            return False

        for word in banned_keywords:
            if word in reason:
                print(f"🚨 [뇌동매매 감지] 진입 근거에 감정적 단어('{word}')가 포함되어 있어 주문을 차단합니다!")
                return False

        print(f"✅ 진입 근거 승인: \"{reason}\"")
        return True

    # =========================================================================
    # 모듈 4: 안전 주문 집행기 (격리 마진 강제 + 지정가 진입 + 마크가격 손절)
    # =========================================================================
    def execute_safe_order(self, symbol: str, calc_data: dict, reason: str, balance: float):
        """격리 마진을 강제 설정하고 진입 주문과 TP/SL을 실행합니다."""
        print("\n[모듈 4] 안전 주문 집행 프로토콜 가동...")
        
        if self.dry_run:
            status = "DRY_RUN_SUCCESS (모의 검증 완료)"
            print("🛡️ [안전 모드(Dry-Run)] 실제 거래소 전송 없이 주문 시뮬레이션을 완료했습니다.")
        else:
            # 실제 거래소 API 호출 구역
            try:
                # 1. 마진 모드를 무조건 격리(ISOLATED)로 강제 변경
                self.exchange.set_margin_mode('isolated', symbol)
                # 2. 레버리지 설정
                self.exchange.set_leverage(self.DEFAULT_LEVERAGE, symbol)
                
                order_side = 'buy' if calc_data['side'] == 'LONG' else 'sell'
                close_side = 'sell' if calc_data['side'] == 'LONG' else 'buy'

                # 3. 본 주문: 지정가(Limit) + Post-Only(무조건 메이커 수수료 적용)
                main_order = self.exchange.create_order(
                    symbol=symbol,
                    type='limit',
                    side=order_side,
                    amount=calc_data['quantity'],
                    price=calc_data['entry'],
                    params={'timeInForce': 'GTX'}  # GTX = Post-Only (지정가 체결 강제)
                )

                # 4. 손절(SL) 주문: 마크 가격(workingType='MARK_PRICE') 기준 강제 청산 전 손절
                sl_order = self.exchange.create_order(
                    symbol=symbol,
                    type='STOP_MARKET',
                    side=close_side,
                    amount=calc_data['quantity'],
                    params={
                        'stopPrice': calc_data['stop_loss'],
                        'reduceOnly': True,             # 포지션 축소 전용(역포지션 진입 방지)
                        'workingType': 'MARK_PRICE'     # 마크 가격 기준 작동 (꼬리 털기 방지)
                    }
                )

                # 5. 익절(TP) 주문: 마크 가격 기준 목표가 청산
                tp_order = self.exchange.create_order(
                    symbol=symbol,
                    type='TAKE_PROFIT_MARKET',
                    side=close_side,
                    amount=calc_data['quantity'],
                    params={
                        'stopPrice': calc_data['take_profit'],
                        'reduceOnly': True,
                        'workingType': 'MARK_PRICE'
                    }
                )
                status = f"LIVE_ORDERED (ID: {main_order['id']})"
                print("🚀 실전 주문 및 마크 가격 기반 TP/SL 등록이 완료되었습니다!")
            except Exception as e:
                print(f"❌ [API 주문 실패]: {e}")
                return

        # =====================================================================
        # 모듈 5: 매매 일지 자동 기록 및 텔레그램 발송
        # =====================================================================
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.JOURNAL_FILE, mode='a', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow([
                now_str, symbol, calc_data['side'], balance,
                calc_data['entry'], calc_data['stop_loss'], calc_data['take_profit'],
                calc_data['stop_distance_pct'], calc_data['rr_ratio'],
                calc_data['max_loss_usd'], calc_data['quantity'], reason, status
            ])
        print(f"📝 [모듈 5] 매매 일지('{self.JOURNAL_FILE}') 자동 기록 완료.")

        msg = (
            f"✅ [원칙 준수 주문 보고]\n"
            f"• 종목: {symbol} ({calc_data['side']})\n"
            f"• 진입가: ${calc_data['entry']} / 손절가: ${calc_data['stop_loss']} / 익절가: ${calc_data['take_profit']}\n"
            f"• 손익비: 1 : {calc_data['rr_ratio']} (통과)\n"
            f"• 최대 허용 손실: -${calc_data['max_loss_usd']} (원금의 1%)\n"
            f"• 산출 수량: {calc_data['quantity']} 개\n"
            f"• 근거: {reason}"
        )
        self.send_telegram_alert(msg)


# =============================================================================
# 실행 테스트 (시나리오 1: 원칙 위반 차단 vs 시나리오 2: 원칙 준수 통과)
# =============================================================================
if __name__ == "__main__":
    assistant = TradingDisciplineAssistant(dry_run=True)
    MY_BALANCE = 2000.0  # 예: 내 선물 계좌 총원금 2,000 USDT

    print("=" * 65)
    print("🧪 [테스트 1] 손익비가 1 : 1.2 로 불리한 자리에서 진입을 시도할 경우")
    print("=" * 65)
    try:
        # 진입 60,000 / 손절 58,800 (-1,200폭) / 익절 61,500 (+1,500폭 -> 손익비 1.25)
        assistant.calculate_and_validate_order(
            balance=MY_BALANCE, side="LONG",
            entry=60000, stop_loss=58800, take_profit=61500
        )
    except ValueError as err:
        print(err)

    print("\n" + "=" * 65)
    print("🧪 [테스트 2] 매뉴얼(손익비 1:2.5, 1% 리스크, 구체적 근거)을 지킨 경우")
    print("=" * 65)
    try:
        # 1. 킬 스위치 확인 (오늘 0연패, 손익 0%)
        if assistant.check_kill_switch(today_Loss_count=0, today_pnl_pct=0.0):
            # 2. 수량 및 손익비 검증: 진입 60,000 / 손절 58,800 (-2%폭) / 익절 63,000 (+5%폭 -> 손익비 1:2.5)
            calc = assistant.calculate_and_validate_order(
                balance=MY_BALANCE, side="LONG",
                entry=60000, stop_loss=58800, take_profit=63000
            )
            # 3. 진입 근거 감사
            entry_reason = "4시간봉 60,000달러 주요 매물대 지지 확인 및 거래량 동반 반등 캔들 마감"
            if assistant.audit_entry_reason(entry_reason):
                # 4. 안전 주문 실행 및 CSV 기록
                assistant.execute_safe_order("BTC/USDT", calc, entry_reason, MY_BALANCE)
    except ValueError as err:
        print(err)