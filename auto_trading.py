import time
import datetime
import asyncio
from pykiwoom.kiwoom import Kiwoom
import keyboard
from telegram import Bot
from telegram.error import TelegramError

# ================== 설정 영역 ==================
kiwoom = Kiwoom()

# ================== 자동로그인 ==================
print("자동로그인 시도 중... (설정이 되어있어야 로그인 창이 안 뜹니다)")
kiwoom.CommConnect(block=True)   # 자동로그인 설정이 되어 있으면 바로 로그인 완료

# 로그인 성공 확인
account_list = kiwoom.GetLoginInfo("ACCNO")
if not account_list:
    print("로그인 실패! 자동로그인 설정을 먼저 확인해주세요.")
    exit()

account = account_list[0]
print(f"✅ 자동로그인 성공! 계좌: {account}")

# 텔레그램 설정 (본인 값으로 변경!)
bot_token = 'YOUR_TELEGRAM_BOT_TOKEN'
chat_id = 'YOUR_CHAT_ID'
bot = Bot(token=bot_token)

buy_list = {}          # {종목코드: (매수가, 수량)}
running = True
max_loss = -5.0        # 손절 (%)
target_profit = 8.0    # 익절 (%)

auto_conditions = []

async def send_telegram(message):
    try:
        await bot.send_message(chat_id=chat_id, text=message)
    except TelegramError as e:
        print(f"텔레그램 전송 실패: {e}")
    except Exception as e:
        print(f"텔레그램 전송 오류: {e}")

def send_telegram_sync(message):
    """동기 텔레그램 전송 (키보드 핫키 등에서 사용)"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 이미 루프가 실행 중인 경우
            asyncio.create_task(bot.send_message(chat_id=chat_id, text=message))
        else:
            # 루프가 없는 경우
            asyncio.run(bot.send_message(chat_id=chat_id, text=message))
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}")

# ================== 현재가 조회 함수 ==================
def get_current_price(code):
    """TR 요청을 통해 현재가 조회"""
    try:
        # OPT10001: 주식기본정보요청
        kiwoom.SetInputValue("종목코드", code)
        kiwoom.CommRqData("주식기본정보", "OPT10001", 0, "0101")
        time.sleep(0.5)  # 응답 대기
        
        # 데이터 가져오기
        data = kiwoom.GetCommDataEx("주식기본정보", "주식기본정보")
        if data and len(data) > 0:
            # 현재가는 인덱스 0 (혹은 컬럼명으로 조회)
            current_price = int(data[0]['현재가']) if '현재가' in data[0] else 0
            return current_price
        
        # 대안: 마스터 데이터 사용
        return kiwoom.GetMasterLastPrice(code)
    except Exception as e:
        print(f"현재가 조회 오류 ({code}): {e}")
        return 0

# ================== 키보드 단축키 ==================
keyboard.add_hotkey('f9', lambda: sell_all())      # F9: 전체 매도
keyboard.add_hotkey('f10', lambda: toggle_running())  # F10: 일시정지/재개

def toggle_running():
    global running
    running = not running
    status = "▶ 실행 중" if running else "⏸ 일시정지"
    print(status)
    send_telegram_sync(f"프로그램 상태: {status}")

def sell_all():
    print("🚨 전체 매도 실행")
    send_telegram_sync("전체 매도 주문 시작")
    for code in list(buy_list.keys()):
        qty = buy_list[code][1]
        if qty > 0:
            try:
                kiwoom.SendOrder("전체매도", "9001", account, 2, code, qty, 0, "03", "")
                send_telegram_sync(f"매도 주문: {code} {qty}주")
            except Exception as e:
                print(f"매도 주문 실패 ({code}): {e}")
    buy_list.clear()

# ================== 조건식 자동 불러오기 ==================
def load_conditions():
    global auto_conditions
    print("조건식 목록 자동 불러오는 중...")
    try:
        kiwoom.GetConditionLoad()
        time.sleep(2)

        condition_list = kiwoom.GetConditionNameList()
        if not condition_list:
            print("등록된 조건식이 없습니다. 키움 HTS에서 조건식을 먼저 만들어 저장하세요.")
            return

        conditions = condition_list.strip(';').split(';')
        auto_conditions = []
        
        print("\n=== 불러온 조건식 목록 ===")
        for cond in conditions:
            if '^' in cond:
                parts = cond.split('^')
                if len(parts) >= 2:
                    idx, name = parts[0], parts[1]
                    print(f"인덱스: {idx} | 이름: {name}")
                    auto_conditions.append({
                        "name": name,
                        "index": int(idx),
                        "buy_amount": 500000   # 매수금액 (원하는 값으로 변경)
                    })
        print(f"총 {len(auto_conditions)}개 조건식 불러오기 완료\n")
    except Exception as e:
        print(f"조건식 불러오기 오류: {e}")

# 실시간 조건 이벤트
def on_receive_real_condition(code, condition_type, condition_name, condition_index):
    try:
        if condition_type == "I" and code not in buy_list:   # 신규 편입
            current_price = get_current_price(code)
            if current_price <= 0:
                print(f"현재가 조회 실패: {code}")
                return
                
            for cond in auto_conditions:
                if cond["name"] == condition_name or cond["index"] == int(condition_index):
                    qty = int(cond["buy_amount"] / current_price)
                    if qty > 0:
                        kiwoom.SendOrder("조건 매수", "9002", account, 1, code, qty, 0, "03", "")
                        buy_list[code] = (current_price, qty)
                        send_telegram_sync(f"✅ 조건 매수!\n종목: {code}\n조건: {condition_name}\n수량: {qty}주 @ {current_price:,}원")
                    break
    except Exception as e:
        print(f"조건 이벤트 처리 오류: {e}")

kiwoom.OnReceiveRealCondition = on_receive_real_condition

# ================== 메인 실행 ==================
load_conditions()

async def main_loop():
    print("=== 조건검색식 자동매매 프로그램 시작 ===")
    await send_telegram("✅ 자동매매 프로그램 시작\n자동로그인 + 조건식 불러오기 완료")

    # 실시간 조건 등록
    for cond in auto_conditions:
        try:
            kiwoom.SendCondition("0100", cond["name"], cond["index"], 1)
            print(f"실시간 조건 등록: {cond['name']}")
            time.sleep(0.5)
        except Exception as e:
            print(f"조건 등록 실패 ({cond['name']}): {e}")

    while True:
        try:
            if not running:
                time.sleep(1)
                continue

            now = datetime.datetime.now()

            # 익절/손절 체크
            for code in list(buy_list.keys()):
                current_price = get_current_price(code)
                if current_price <= 0:
                    continue
                    
                buy_price, qty = buy_list[code]
                profit_rate = (current_price - buy_price) / buy_price * 100 if buy_price > 0 else 0

                if profit_rate >= target_profit:
                    kiwoom.SendOrder("익절", "9003", account, 2, code, qty, 0, "03", "")
                    await send_telegram(f"🎉 익절: {code} +{profit_rate:.1f}%")
                    del buy_list[code]
                elif profit_rate <= max_loss:
                    kiwoom.SendOrder("손절", "9004", account, 2, code, qty, 0, "03", "")
                    await send_telegram(f"⛔ 손절: {code} {profit_rate:.1f}%")
                    del buy_list[code]

            print(f"실행 중 | 보유: {len(buy_list)}종목 | {now.strftime('%H:%M:%S')}", end="\r")
            time.sleep(1)

        except KeyboardInterrupt:
            print("\n프로그램 종료")
            break
        except Exception as e:
            print(f"오류: {e}")
            time.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("\n프로그램 종료")
