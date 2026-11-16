"""
FYERS daily TSI scanner (signals only - it never places an order).

After the daily close it looks at the daily candles of every stock in stocks.txt and sends ONE Telegram message
listing the stocks whose TSI(25,13,13) turned BULLISH on the latest completed daily candle
(TSI crossed above its signal line: yesterday TSI <= signal, today TSI > signal).

Commands (run from GitHub Actions, workflow "fyers-daily"):
  --login-link   send the FYERS login link to Telegram (do this first each day, after 6:30 AM)
  --check        log in, read ONE stock, print the candles it got (use for the first test)
  --scan         log in, scan every stock in stocks.txt, send the result to Telegram

Login: FYERS tokens expire every day at 6:30 AM. Open the login link, log in, then copy the WHOLE address of the page
you land on (it contains auth_code=...) and paste it into the "auth_code" box when you run the workflow.
"""
import hashlib
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, urlparse

import requests

IST = timezone(timedelta(hours=5, minutes=30))


def env(name, default=""):
    v = os.getenv(name)
    return default if v is None or v == "" else v.strip()


APP_ID = env("FYERS_APP_ID")
APP_SECRET = env("FYERS_APP_SECRET")
REDIRECT_URI = env("FYERS_REDIRECT_URI")
AUTH_BASE = env("FYERS_AUTH_BASE", "https://api-t1.fyers.in/api/v3")
DATA_BASE = env("FYERS_DATA_BASE", "https://api-t1.fyers.in/data")   # history endpoint host: confirm with --check
TG_TOKEN = env("TG_TOKEN")
TG_CHAT_ID = env("TG_CHAT_ID")
STOCKS_FILE = env("STOCKS_FILE", "stocks.txt")
TSI_LONG, TSI_SHORT, TSI_SIGNAL = 25, 13, 13
DAYS_BACK = int(env("DAYS_BACK", "360"))          # FYERS allows up to 366 days of daily candles per request
PACE_SECONDS = float(env("PACE_SECONDS", "1.5"))  # keeps us under 5 calls/second and 50 calls/minute (standard plan)
CLOSE_READY_IST = (15, 45)                        # a daily candle dated today is trusted only after this IST time
MIN_CANDLES = 120


# ----------------------------- maths (same as the BTC bot) -----------------------------
def ema(values, n):
    k = 2.0 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def tsi_series(closes):
    mom = [0.0] + [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    a = ema(ema(mom, TSI_LONG), TSI_SHORT)
    b = ema(ema([abs(x) for x in mom], TSI_LONG), TSI_SHORT)
    tsi = [100.0 * x / y if y else 0.0 for x, y in zip(a, b)]
    return tsi, ema(tsi, TSI_SIGNAL)


def bullish_today(closes):
    """(True/False, tsi, signal) on the LAST candle."""
    tsi, sig = tsi_series(closes)
    prev, cur = tsi[-2] - sig[-2], tsi[-1] - sig[-1]
    return (prev <= 0 < cur), tsi[-1], sig[-1]


# ----------------------------- Telegram -----------------------------
def send(text):
    print(text, flush=True)
    for i in range(0, len(text), 3800):
        try:
            r = requests.post("https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN,
                              data={"chat_id": TG_CHAT_ID, "text": text[i:i + 3800]}, timeout=20)
            if not r.json().get("ok"):
                print("Telegram error:", r.text, flush=True)
        except Exception as e:
            print("Telegram send failed:", e, flush=True)


# ----------------------------- FYERS -----------------------------
class FyersError(Exception):
    pass


class RateLimited(FyersError):
    pass


def need_settings():
    missing = [n for n, v in (("FYERS_APP_ID", APP_ID), ("FYERS_APP_SECRET", APP_SECRET),
                              ("FYERS_REDIRECT_URI", REDIRECT_URI)) if not v]
    if missing:
        raise SystemExit("Missing settings: %s (add them as GitHub secrets / variables)." % ", ".join(missing))


def login_link():
    need_settings()
    return "%s/generate-authcode?client_id=%s&redirect_uri=%s&response_type=code&state=tsi" % (
        AUTH_BASE, quote(APP_ID, safe=""), quote(REDIRECT_URI, safe=""))


def auth_code_from(text):
    """Accepts the whole address-bar URL or just the code."""
    text = (text or "").strip().strip("\"'")
    if "auth_code=" in text:
        q = parse_qs(urlparse(text).query)
        if q.get("auth_code"):
            return q["auth_code"][0]
        return text.split("auth_code=")[1].split("&")[0]
    return text


def get_token(code):
    need_settings()
    if not code:
        raise SystemExit("No auth_code given. Run --login-link first, log in, and paste the whole page address into the auth_code box.")
    body = {"grant_type": "authorization_code",
            "appIdHash": hashlib.sha256(("%s:%s" % (APP_ID, APP_SECRET)).encode()).hexdigest(),
            "code": auth_code_from(code)}
    r = requests.post(AUTH_BASE + "/validate-authcode", json=body, timeout=30)
    try:
        j = r.json()
    except Exception:
        raise FyersError("Login failed: HTTP %s %s" % (r.status_code, r.text[:200]))
    if j.get("s") != "ok" or not j.get("access_token"):
        raise FyersError("Login failed: %s (the code works once and only for a few minutes - get a new login link)"
                         % (j.get("message") or j))
    return j["access_token"]


def history(token, symbol, resolution="D", days=None):
    today = datetime.now(IST).date()
    params = {"symbol": symbol, "resolution": resolution, "date_format": "1",
              "range_from": (today - timedelta(days=days or DAYS_BACK)).isoformat(), "range_to": today.isoformat(), "cont_flag": "1"}
    headers = {"Authorization": "%s:%s" % (APP_ID, token)}
    last = None
    for attempt in range(2):
        try:
            r = requests.get(DATA_BASE + "/history", params=params, headers=headers, timeout=30)
        except requests.RequestException as e:
            last = e
            time.sleep(3)
            continue
        if r.status_code == 429:
            raise RateLimited("FYERS says too many requests (429)")
        try:
            j = r.json()
        except Exception:
            raise FyersError("HTTP %s %s" % (r.status_code, r.text[:150]))
        if j.get("code") == -429:
            raise RateLimited("FYERS says too many requests (-429)")
        if j.get("s") != "ok":
            raise FyersError(str(j.get("message") or j)[:200])
        return j.get("candles") or []
    raise FyersError("network problem: %s" % last)


def completed_closes(candles, now=None):
    """[(date, close)] of finished daily candles, oldest first."""
    now = now or datetime.now(IST)
    rows = []
    for c in sorted(candles, key=lambda x: x[0]):
        day = datetime.fromtimestamp(int(c[0]), IST).date()
        rows.append((day, float(c[4])))
    if rows and rows[-1][0] == now.date() and (now.hour, now.minute) < CLOSE_READY_IST:
        rows = rows[:-1]                       # today's candle is still forming
    return rows


def short(sym):
    return sym.replace("NSE:", "").replace("-EQ", "")


def symbols():
    out = []
    with open(STOCKS_FILE) as f:
        for line in f:
            s = line.split("#")[0].strip().upper()
            if not s:
                continue
            out.append(s if ":" in s else "NSE:%s-EQ" % s)
    return out


# ----------------------------- commands -----------------------------
def cmd_check(token):
    sym = symbols()[0]
    candles = history(token, sym)
    rows = completed_closes(candles)
    print("Symbol %s: %d daily candles received, %d finished." % (sym, len(candles), len(rows)))
    if candles:
        print("First candle epoch %s -> %s IST | last candle epoch %s -> %s IST" % (
            candles[0][0], datetime.fromtimestamp(int(candles[0][0]), IST), candles[-1][0],
            datetime.fromtimestamp(int(candles[-1][0]), IST)))
    if rows:
        print("Latest finished daily candle: %s close %.2f" % rows[-1])
    if len(rows) >= MIN_CANDLES:
        ok, t, s = bullish_today([c for _, c in rows])
        print("TSI %.2f vs signal %.2f | bullish crossover on the latest day: %s" % (t, s, ok))
    print("Check finished: no orders exist in this program.")


def cmd_scan(token):
    syms = symbols()
    hits, errors, data_days, scanned = [], [], {}, 0
    stopped = None
    for n, sym in enumerate(syms):
        if n:
            time.sleep(PACE_SECONDS)
        try:
            rows = completed_closes(history(token, sym))
        except RateLimited as e:
            stopped = "%s (stopped after %d of %d stocks)" % (e, n, len(syms))
            break
        except FyersError as e:
            errors.append("%s: %s" % (short(sym), e))
            continue
        if len(rows) < MIN_CANDLES:
            errors.append("%s: only %d daily candles" % (short(sym), len(rows)))
            continue
        scanned += 1
        day = rows[-1][0]
        data_days[day] = data_days.get(day, 0) + 1
        ok, t, s = bullish_today([c for _, c in rows])
        if ok:
            hits.append((short(sym), rows[-1][1], t, s))
    if not data_days:
        send("⚠️ Daily TSI scan: no usable data.\n" + "\n".join(errors[:10]) + ("\n" + stopped if stopped else ""))
        return
    day = max(data_days)
    stale = sum(c for d, c in data_days.items() if d != day)
    head = "📈 Daily TSI bullish crossover | close of %s | %d stocks scanned" % (day.strftime("%d %b %Y"), scanned)
    if hits:
        lines = ["%s  close %.2f  (TSI %.1f vs signal %.1f)" % h for h in sorted(hits)]
        text = "%s\n%d stock(s) turned bullish:\n%s" % (head, len(hits), "\n".join(lines))
    else:
        text = "%s\nNo stock turned bullish on this close." % head
    if stale:
        text += "\n⚠️ %d stock(s) had older data than %s (holiday or no trade)." % (stale, day.strftime("%d %b"))
    if stopped:
        text += "\n⚠️ " + stopped
    if errors:
        text += "\n⚠️ %d problem(s): %s" % (len(errors), "; ".join(errors[:6]))
    send(text)


def main():
    if "--login-link" in sys.argv:
        send("🔑 FYERS login (valid for today):\n%s\n\nLog in, then copy the WHOLE address of the page you land on and paste it "
             "into the auth_code box of the fyers-daily workflow (choose cmd = scan)." % login_link())
        return
    token = get_token(env("AUTH_CODE"))
    if "--check" in sys.argv:
        cmd_check(token)
    elif "--scan" in sys.argv:
        cmd_scan(token)
    else:
        raise SystemExit("Use --login-link, --check or --scan.")


if __name__ == "__main__":
    try:
        main()
    except (FyersError, SystemExit) as e:
        if isinstance(e, SystemExit) and not e.code:
            raise
        msg = str(e.code if isinstance(e, SystemExit) else e)
        send("❌ FYERS daily scan problem: %s" % msg)
        raise SystemExit(1)
