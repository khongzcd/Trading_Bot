"""
Bitkub Multi-Coin Scanner Bot (RSI + EMA filter) พร้อมแจ้งเตือน Discord Webhook
- ดึง ticker ทุกเหรียญด้วยการยิง API ครั้งเดียว แล้วกรองเฉพาะคู่ที่ลงท้าย/ขึ้นต้นด้วย QUOTE (เช่น THB)
- คัดเหรียญตามสภาพคล่อง (volume) ก่อน แล้วค่อยดึงแท่งเทียนทีละเหรียญพร้อม Rate Limiter
- คำนวณ RSI/EMA ทุกเหรียญ แจ้งเตือนเฉพาะเหรียญที่น่าสนใจที่สุด
- try-except ราย-เหรียญ + สรุปผลสแกนทุกรอบ
- ค่าลับทั้งหมดอ่านจาก .env, ค่าเริ่มต้นเป็น PAPER TRADING (LIVE_TRADING=false)
"""
import os
import time
import logging
from datetime import datetime

import ccxt
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------- ตั้งค่า (จาก .env) ----------------
EXCHANGE_ID = "bitkub"
API_KEY = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"

QUOTE = os.getenv("QUOTE", "THB").upper()           # สกุลที่ใช้สแกน: THB (หรือ BTC ถ้า Bitkub มีคู่นั้น)
TIMEFRAME = os.getenv("TIMEFRAME", "15m")           # 1m,5m,15m,30m,1h,4h,1d
RSI_PERIOD = int(os.getenv("RSI_PERIOD", 14))
RSI_BUY = float(os.getenv("RSI_BUY", 30))
RSI_SELL = float(os.getenv("RSI_SELL", 70))
EMA_PERIOD = int(os.getenv("EMA_PERIOD", 200))

# --- Scanner / Rate limit ---
MIN_QUOTE_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME", 500000))  # ปริมาณซื้อขาย 24 ชม. ขั้นต่ำ (สกุล quote)
MAX_COINS_PER_SCAN = int(os.getenv("MAX_COINS_PER_SCAN", 40))    # สแกนสูงสุดกี่เหรียญ/รอบ (เรียงตาม volume)
REQUEST_DELAY = max(1.0, float(os.getenv("REQUEST_DELAY", 1.0)))  # หน่วงต่อ 1 คำขอแท่งเทียน (ขั้นต่ำ 1 วินาที)
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 120))             # พักระหว่างรอบสแกน (วินาที)
TOP_ALERTS = int(os.getenv("TOP_ALERTS", 5))                     # แจ้งเตือนสัญญาณ BUY สูงสุดกี่เหรียญ/รอบ
MAX_RETRIES = 2

# --- Risk ---
MAX_ORDER_QUOTE = float(os.getenv("MAX_ORDER_QUOTE", 100))       # วงเงินต่อออร์เดอร์
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", 3))               # ถือพร้อมกันสูงสุดกี่เหรียญ
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", 2))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", 4))
MAX_DAILY_LOSS_QUOTE = float(os.getenv("MAX_DAILY_LOSS_QUOTE", 500))

BASE_URL = "https://api.bitkub.com"
TF_MAP = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "1D"}
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

# ---------------- สถานะ ----------------
positions = {}        # symbol (เช่น "BTC_THB") -> {"entry", "amount"}
alerted = {}          # symbol -> (signal, candle_time) กันแจ้งเตือนซ้ำในแท่งเดียวกัน
daily_loss = 0.0
daily_date = datetime.now().date()
session = requests.Session()
exchange = None       # สร้างเมื่อใช้ LIVE เท่านั้น


# ---------------- Utilities ----------------
def notify(message):
    """ส่งข้อความเข้า Discord Webhook (ใส่เวลาให้อัตโนมัติ)"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info(message.replace("\n", " | "))
    if not WEBHOOK_URL:
        return
    try:
        session.post(WEBHOOK_URL, json={"content": f"{message}\n🕒 {now}"[:1900]}, timeout=10)
    except requests.RequestException as e:
        log.error("ส่ง Discord ไม่สำเร็จ: %s", e)


def http_get(url, params=None):
    """GET พร้อม retry เบาๆ และหน่วงเมื่อโดน 429 (Rate Limit)"""
    for attempt in range(MAX_RETRIES + 1):
        r = session.get(url, params=params, timeout=15)
        if r.status_code == 429:
            wait = 5 * (attempt + 1)
            log.warning("โดน Rate Limit (429) พัก %s วินาที", wait)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Rate limited ต่อเนื่อง")


# ---------------- ดึงข้อมูลตลาด ----------------
def fetch_all_tickers():
    """
    ดึง ticker ทุกคู่เหรียญด้วยคำขอเดียว แล้วกรองเฉพาะ QUOTE ที่ตั้งไว้
    รองรับทั้ง v3 (list, symbol แบบ BTC_THB) และ v1 เดิม (dict, key แบบ THB_BTC)
    คืนค่า: {"BTC_THB": {"last": float, "volume": float, "change": float}, ...}
    """
    result = {}
    try:
        data = http_get(f"{BASE_URL}/api/v3/market/ticker")
        for t in data if isinstance(data, list) else []:
            sym = t["symbol"].upper()
            if sym.endswith("_" + QUOTE):
                result[sym] = {"last": float(t["last"]),
                               "volume": float(t.get("quote_volume", 0) or 0),
                               "change": float(t.get("percent_change", 0) or 0)}
    except Exception as e:
        log.warning("v3 ticker ใช้ไม่ได้ (%s) ลอง endpoint เดิม", e)

    if not result:  # fallback: /api/market/ticker -> {"THB_BTC": {...}}
        data = http_get(f"{BASE_URL}/api/market/ticker")
        for key, t in data.items():
            if key.upper().startswith(QUOTE + "_"):
                sym = f"{key.split('_', 1)[1]}_{QUOTE}".upper()
                result[sym] = {"last": float(t["last"]),
                               "volume": float(t.get("quoteVolume", 0) or 0),
                               "change": float(t.get("percentChange", 0) or 0)}
    return result


def fetch_candles(symbol):
    """ดึงแท่งเทียนย้อนหลังของเหรียญเดียวจาก Bitkub TradingView endpoint -> DataFrame"""
    now = int(time.time())
    count = max(EMA_PERIOD, RSI_PERIOD) + 60
    data = http_get(f"{BASE_URL}/tradingview/history", {
        "symbol": symbol, "resolution": TF_MAP[TIMEFRAME],
        "from": now - count * TF_SECONDS[TIMEFRAME], "to": now})
    if data.get("s") != "ok" or not data.get("c"):
        raise ValueError(f"ไม่มีข้อมูลแท่งเทียน ({data.get('s')})")
    return pd.DataFrame({"time": data["t"], "close": data["c"]})


# ---------------- อินดิเคเตอร์ / สัญญาณ ----------------
def calc_rsi(close, period):
    """คำนวณ RSI แบบ Wilder smoothing"""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, 1e-12))


def analyze(df):
    """
    วิเคราะห์จากแท่งที่ปิดแล้ว (รองสุดท้าย):
    BUY = RSI < RSI_BUY และราคาเหนือ EMA | SELL = RSI > RSI_SELL | ไม่เข้าเงื่อนไข = HOLD
    คืนค่า (signal, rsi, ema, candle_time)
    """
    if len(df) < EMA_PERIOD // 2 + 2:
        raise ValueError(f"ข้อมูลน้อยเกินไป ({len(df)} แท่ง)")
    rsi = calc_rsi(df["close"], RSI_PERIOD).iloc[-2]
    ema = df["close"].ewm(span=EMA_PERIOD, adjust=False).mean().iloc[-2]
    price, ctime = df["close"].iloc[-2], int(df["time"].iloc[-2])
    if rsi < RSI_BUY and price > ema:
        return "BUY", float(rsi), float(ema), ctime
    if rsi > RSI_SELL:
        return "SELL", float(rsi), float(ema), ctime
    return "HOLD", float(rsi), float(ema), ctime


# ---------------- ออร์เดอร์ (Paper / Live) ----------------
def ccxt_symbol(symbol):
    """แปลง BTC_THB -> BTC/THB สำหรับ ccxt"""
    return symbol.replace("_", "/")


def open_position(symbol, price, rsi):
    """เปิดออร์เดอร์ซื้อ (จำกัดวงเงินต่อออร์เดอร์)"""
    amount = MAX_ORDER_QUOTE / price
    if LIVE_TRADING:
        amount = float(exchange.amount_to_precision(ccxt_symbol(symbol), amount))
        exchange.create_market_buy_order(ccxt_symbol(symbol), amount)
    positions[symbol] = {"entry": price, "amount": amount}
    mode = "LIVE" if LIVE_TRADING else "PAPER"
    notify(f"🟢 **เปิดออร์เดอร์ BUY** [{symbol}] ({mode})\nราคา {price:,.4f} | จำนวน {amount:.6f} | RSI {rsi:.1f}\n"
           f"SL {price * (1 - STOP_LOSS_PCT / 100):,.4f} | TP {price * (1 + TAKE_PROFIT_PCT / 100):,.4f}")


def close_position(symbol, price, reason):
    """ปิดออร์เดอร์ (ขาย) และบันทึกกำไร/ขาดทุน"""
    global daily_loss
    pos = positions[symbol]
    if LIVE_TRADING:
        exchange.create_market_sell_order(ccxt_symbol(symbol), pos["amount"])
    pnl = (price - pos["entry"]) * pos["amount"]
    if pnl < 0:
        daily_loss += -pnl
    del positions[symbol]
    mode = "LIVE" if LIVE_TRADING else "PAPER"
    notify(f"🔴 **ปิดออร์เดอร์ SELL** [{symbol}] ({mode}) เหตุผล: {reason}\n"
           f"ราคา {price:,.4f} | PnL {pnl:+,.2f} {QUOTE}")


def check_risk(tickers):
    """ตรวจ Stop Loss / Take Profit ของทุกสถานะที่ถือ โดยใช้ราคาจาก ticker รอบเดียว (ไม่เสียคำขอเพิ่ม)"""
    for symbol in list(positions):
        try:
            if symbol not in tickers:
                continue
            price = tickers[symbol]["last"]
            change = (price - positions[symbol]["entry"]) / positions[symbol]["entry"] * 100
            if change <= -STOP_LOSS_PCT:
                close_position(symbol, price, f"Stop Loss ({change:.2f}%)")
            elif change >= TAKE_PROFIT_PCT:
                close_position(symbol, price, f"Take Profit ({change:.2f}%)")
        except Exception as e:
            log.error("เช็คความเสี่ยง %s ผิดพลาด: %s", symbol, e)


# ---------------- Scanner ----------------
def scan_once():
    """
    หนึ่งรอบสแกน:
    1) ดึง ticker ทั้งตลาดครั้งเดียว  2) เช็ค SL/TP  3) คัดเหรียญตาม volume
    4) ดึงแท่งเทียนทีละเหรียญ (หน่วงเวลา, try-except ราย-เหรียญ)  5) แจ้งเตือน + สรุปผล
    """
    global daily_loss, daily_date
    started = time.time()
    if datetime.now().date() != daily_date:
        daily_date, daily_loss = datetime.now().date(), 0.0

    tickers = fetch_all_tickers()
    check_risk(tickers)

    # คัดเหรียญที่สภาพคล่องพอ + เหรียญที่ถืออยู่ (ต้องวิเคราะห์สัญญาณขายด้วย)
    candidates = sorted((s for s, t in tickers.items() if t["volume"] >= MIN_QUOTE_VOLUME),
                        key=lambda s: tickers[s]["volume"], reverse=True)[:MAX_COINS_PER_SCAN]
    for s in positions:
        if s in tickers and s not in candidates:
            candidates.append(s)

    buys, sells, errors, scanned = [], [], [], 0
    for i, symbol in enumerate(candidates):
        if i:
            time.sleep(REQUEST_DELAY)  # Rate limiter
        try:
            signal, rsi, ema, ctime = analyze(fetch_candles(symbol))
            scanned += 1
            if signal == "HOLD" or alerted.get(symbol) == (signal, ctime):
                continue  # ไม่มีสัญญาณ หรือแจ้งไปแล้วในแท่งนี้
            price = tickers[symbol]["last"]
            if signal == "BUY":
                buys.append((rsi, symbol, price, ctime))
            elif signal == "SELL" and symbol in positions:
                alerted[symbol] = (signal, ctime)
                sells.append(symbol)
                notify(f"📉 สัญญาณ SELL [{symbol}] ราคา {price:,.4f} | RSI {rsi:.1f}")
                close_position(symbol, price, "RSI overbought")
        except Exception as e:  # เหรียญเดียวพัง ไม่ล้มทั้งบอท
            errors.append(symbol)
            log.error("สแกน %s ล้มเหลว: %s", symbol, e)

    # เลือกเฉพาะ BUY ที่น่าสนใจที่สุด (RSI ต่ำสุด) ตาม TOP_ALERTS
    bought = 0
    for rsi, symbol, price, ctime in sorted(buys)[:TOP_ALERTS]:
        alerted[symbol] = ("BUY", ctime)
        notify(f"📈 สัญญาณ BUY [{symbol}] ราคา {price:,.4f} | RSI {rsi:.1f} | "
               f"Vol24h {tickers[symbol]['volume']:,.0f} {QUOTE} | 24h {tickers[symbol]['change']:+.2f}%")
        if symbol in positions or len(positions) >= MAX_POSITIONS:
            continue
        if daily_loss >= MAX_DAILY_LOSS_QUOTE:
            log.warning("ขาดทุนรายวันถึงขีดจำกัด งดเปิดออร์เดอร์ใหม่")
            continue
        try:
            open_position(symbol, price, rsi)
            bought += 1
        except Exception as e:
            log.error("เปิดออร์เดอร์ %s ล้มเหลว: %s", symbol, e)

    log.info("สรุปรอบสแกน: ตลาด %d คู่ | คัดมา %d | สแกนสำเร็จ %d | ผิดพลาด %d | BUY %d | SELL %d | "
             "ถืออยู่ %d | ใช้เวลา %.1fs", len(tickers), len(candidates), scanned, len(errors),
             len(buys), len(sells), len(positions), time.time() - started)
    return {"scanned": scanned, "errors": len(errors), "buys": len(buys), "sells": len(sells)}


def main():
    """จุดเริ่มต้น: วนสแกนตลอดจนกด Ctrl+C และส่งสรุปสถานะเป็นระยะ"""
    global exchange
    if TIMEFRAME not in TF_MAP:
        raise SystemExit(f"TIMEFRAME ไม่รองรับ: {TIMEFRAME} (ใช้ {list(TF_MAP)})")
    if LIVE_TRADING:
        exchange = ccxt.bitkub({"apiKey": API_KEY, "secret": API_SECRET, "enableRateLimit": True})
    mode = "LIVE (เงินจริง!)" if LIVE_TRADING else "PAPER (จำลอง)"
    notify(f"🤖 Multi-Coin Scanner เริ่มทำงาน | Bitkub *_{QUOTE} | {TIMEFRAME} | โหมด {mode}")

    scans = 0
    while True:
        try:
            stats = scan_once()
            scans += 1
            if scans % 30 == 0:  # ส่งสรุปสถานะเข้า Discord ทุก 30 รอบ
                notify(f"✅ Heartbeat: สแกนแล้ว {scans} รอบ | รอบล่าสุดสำเร็จ {stats['scanned']} "
                       f"เหรียญ ผิดพลาด {stats['errors']} | ถืออยู่ {len(positions)}")
        except KeyboardInterrupt:
            notify("⏹️ บอทหยุดทำงาน")
            break
        except Exception as e:  # ticker ล่มทั้งรอบ: ข้ามไปรอบหน้า
            log.error("รอบสแกนล้มเหลว: %s", e)
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
