"""
Trading Bot (RSI + EMA filter) พร้อมแจ้งเตือน Discord Webhook
- ใช้ ccxt ดึงข้อมูลราคา
- ค่าลับทั้งหมดอ่านจาก .env
- มี Stop Loss / Take Profit / จำกัดวงเงินต่อออร์เดอร์
- ค่าเริ่มต้นเป็น PAPER TRADING (จำลอง ไม่ใช้เงินจริง)
"""
import os
import time
import logging
from datetime import datetime

import ccxt
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()  # โหลดค่าจากไฟล์ .env

# ---------------- ตั้งค่า (จาก .env) ----------------
EXCHANGE_ID = os.getenv("EXCHANGE", "binance")          # binance / bitkub
API_KEY = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
SYMBOL = os.getenv("SYMBOL", "BTC/USDT")
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"

RSI_PERIOD = int(os.getenv("RSI_PERIOD", 14))
RSI_BUY = float(os.getenv("RSI_BUY", 30))     # RSI ต่ำกว่านี้ = oversold
RSI_SELL = float(os.getenv("RSI_SELL", 70))   # RSI สูงกว่านี้ = overbought
EMA_PERIOD = int(os.getenv("EMA_PERIOD", 200))

MAX_ORDER_QUOTE = float(os.getenv("MAX_ORDER_QUOTE", 20))  # วงเงินสูงสุดต่อออร์เดอร์ (สกุล quote)
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", 2))       # ขาดทุนเกินกี่ % ให้ตัดขาดทุน
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", 4))   # กำไรถึงกี่ % ให้ขายทำกำไร
MAX_DAILY_LOSS_QUOTE = float(os.getenv("MAX_DAILY_LOSS_QUOTE", 50))  # ขาดทุนสะสมต่อวันสูงสุด
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", 60))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

# สถานะของบอท
position = None          # {"entry": ราคาเข้า, "amount": จำนวนเหรียญ}
daily_loss = 0.0
daily_date = datetime.now().date()


def create_exchange():
    """สร้างการเชื่อมต่อ exchange ผ่าน ccxt"""
    cls = getattr(ccxt, EXCHANGE_ID)
    return cls({"apiKey": API_KEY, "secret": API_SECRET, "enableRateLimit": True})


def notify(message):
    """ส่งข้อความเข้า Discord ผ่าน Webhook (พร้อมคู่เหรียญและเวลา)"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"**[{SYMBOL}]** {message}\n🕒 {now}"
    log.info(message)
    if not WEBHOOK_URL:
        return
    try:
        requests.post(WEBHOOK_URL, json={"content": text}, timeout=10)
    except requests.RequestException as e:
        log.error("ส่ง Discord ไม่สำเร็จ: %s", e)


def fetch_data(exchange):
    """ดึงราคาย้อนหลัง (OHLCV) มาเป็น DataFrame"""
    limit = max(EMA_PERIOD, RSI_PERIOD) + 100
    ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=limit)
    return pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])


def calc_rsi(close, period):
    """คำนวณ RSI แบบ Wilder smoothing"""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-12)
    return 100 - 100 / (1 + rs)


def get_signal(df):
    """
    กำหนดสัญญาณ:
    - BUY : RSI < RSI_BUY และราคาอยู่เหนือ EMA (เทรนด์ขาขึ้น กรองสัญญาณหลอก)
    - SELL: RSI > RSI_SELL
    - อื่นๆ: HOLD
    ใช้แท่งที่ปิดแล้ว (แท่งรองสุดท้าย) เพื่อลดสัญญาณหลอก
    """
    rsi = calc_rsi(df["close"], RSI_PERIOD).iloc[-2]
    ema = df["close"].ewm(span=EMA_PERIOD, adjust=False).mean().iloc[-2]
    price = df["close"].iloc[-2]
    if rsi < RSI_BUY and price > ema:
        return "BUY", rsi
    if rsi > RSI_SELL:
        return "SELL", rsi
    return "HOLD", rsi


def open_position(exchange, price):
    """เปิดออร์เดอร์ซื้อ โดยจำกัดวงเงินตาม MAX_ORDER_QUOTE"""
    global position
    amount = MAX_ORDER_QUOTE / price
    if LIVE_TRADING:
        amount = float(exchange.amount_to_precision(SYMBOL, amount))
        exchange.create_market_buy_order(SYMBOL, amount)
    position = {"entry": price, "amount": amount}
    mode = "LIVE" if LIVE_TRADING else "PAPER"
    notify(f"🟢 เปิดออร์เดอร์ BUY ({mode}) ราคา {price:.4f} จำนวน {amount:.6f}\n"
           f"SL: {price * (1 - STOP_LOSS_PCT / 100):.4f} | TP: {price * (1 + TAKE_PROFIT_PCT / 100):.4f}")


def close_position(exchange, price, reason):
    """ปิดออร์เดอร์ (ขาย) และบันทึกกำไร/ขาดทุน"""
    global position, daily_loss
    if LIVE_TRADING:
        exchange.create_market_sell_order(SYMBOL, position["amount"])
    pnl = (price - position["entry"]) * position["amount"]
    if pnl < 0:
        daily_loss += -pnl
    mode = "LIVE" if LIVE_TRADING else "PAPER"
    notify(f"🔴 ปิดออร์เดอร์ SELL ({mode}) เหตุผล: {reason}\n"
           f"ราคา {price:.4f} | PnL: {pnl:+.4f}")
    position = None


def check_risk(exchange, price):
    """ตรวจ Stop Loss / Take Profit ของออร์เดอร์ที่เปิดอยู่"""
    change = (price - position["entry"]) / position["entry"] * 100
    if change <= -STOP_LOSS_PCT:
        close_position(exchange, price, f"Stop Loss ({change:.2f}%)")
    elif change >= TAKE_PROFIT_PCT:
        close_position(exchange, price, f"Take Profit ({change:.2f}%)")


def run_once(exchange):
    """รอบการทำงานหนึ่งรอบ: ดึงข้อมูล -> เช็คความเสี่ยง -> เช็คสัญญาณ -> ส่งออร์เดอร์"""
    global daily_loss, daily_date
    if datetime.now().date() != daily_date:  # รีเซ็ตขาดทุนรายวัน
        daily_date, daily_loss = datetime.now().date(), 0.0

    df = fetch_data(exchange)
    price = exchange.fetch_ticker(SYMBOL)["last"]

    if position:
        check_risk(exchange, price)
        if position is None:
            return

    signal, rsi = get_signal(df)
    log.info("ราคา %.4f | RSI %.2f | สัญญาณ %s", price, rsi, signal)

    if signal == "BUY" and position is None:
        if daily_loss >= MAX_DAILY_LOSS_QUOTE:
            log.warning("ขาดทุนรายวันถึงขีดจำกัด งดเปิดออร์เดอร์ใหม่")
            return
        notify(f"📈 สัญญาณ BUY | ราคา {price:.4f} | RSI {rsi:.2f}")
        open_position(exchange, price)
    elif signal == "SELL" and position:
        notify(f"📉 สัญญาณ SELL | ราคา {price:.4f} | RSI {rsi:.2f}")
        close_position(exchange, price, "สัญญาณ RSI overbought")


def main():
    """จุดเริ่มต้นโปรแกรม: วนลูปทำงานตลอดจนกด Ctrl+C"""
    exchange = create_exchange()
    mode = "LIVE (เงินจริง!)" if LIVE_TRADING else "PAPER (จำลอง)"
    notify(f"🤖 บอทเริ่มทำงาน | {EXCHANGE_ID} | {TIMEFRAME} | โหมด {mode}")
    while True:
        try:
            run_once(exchange)
        except KeyboardInterrupt:
            notify("⏹️ บอทหยุดทำงาน")
            break
        except Exception as e:  # กันบอทล่มจากปัญหาเครือข่าย/API
            log.error("เกิดข้อผิดพลาด: %s", e)
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()

