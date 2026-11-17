"""
TSI signals for Indian stocks on FYERS data (signals only - this program never places an order).

  * TSI(25,13,13) is calculated on 45-minute candles and READ ON THE 1-HOUR CHART exactly as TradingView draws it:
    each 1h candle shows the TSI of the 45m candle that OPENED last before the 1h candle closed (mapping "B").
    NSE candles start at 9:15, so the 1h candles are 9:15, 10:15 ... 14:15 and a short 15:15-15:30 one, and the 45m
    candles are 9:15, 10:00, 10:45, 11:30, 12:15, 13:00, 13:45, 14:30 and a short 15:15-15:30 one. The readings
    therefore become known at 10:45, 11:30, 12:15, 13:45, 14:30, 15:15 and 15:30 IST.
  * SYSTEM 1: BULLISH crossover (TSI crosses above its signal line) on any stock in stocks.txt -> Telegram alert.
  * SYSTEM 2: EMA 5 (source close) on 2-hour candles, compared with every finished 1h candle close:
      TSI bullish -> alert when a 1h candle CLOSES BELOW the 2h 5 EMA (first close on that side)
      TSI bearish -> alert when a 1h candle CLOSES ABOVE the 2h 5 EMA (first close on that side)
    (the TSI state used is the latest 45m reading already finished at that moment.)
  * Alerts found in one run are combined into one message with an "I bought SYMBOL" button per stock.
  * You tell the bot about a trade by just sending the stock name (or pressing that button): RELIANCE
    The bot tracks the stock FROM ITS LATEST BULLISH SIGNAL (a bullish TSI crossover or a 1h close above the
    2h 5 EMA, whichever is newer; its time and price = the entry).
  * Every morning, until you press Out, it sends how each tracked stock performs since that entry signal.
  * When the 1h TSI turns BEARISH on a stock you are in, it sends "TIME TO EXIT" with an "I am out" button and a
    "Still holding" button; EMA alerts on a tracked stock carry the same two buttons. Pressing "I am out"
    (or sending: out RELIANCE) makes it stop tracking that stock.
  * Intraday or overnight is your decision: positions stay tracked until you press Out. Other messages: list | help

Commands (GitHub workflow "tsi-stocks"):  --login-link | --login | --refresh | --check | --scan
The FYERS token (valid until 6:30 AM next day) is kept in the GitHub Actions cache by the workflow, never in the repo.
"""
import bisect
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

import fyers_daily as FD
from fyers_daily import IST, env, send

STATE_FILE = env("STOCK_STATE_FILE", "stock_state.json")
TOKEN_FILE = env("FYERS_TOKEN_FILE", ".token/fyers_token.json")
REFRESH_FILE = env("FYERS_REFRESH_FILE", ".token/fyers_refresh.json")
NEW_TOKEN_MARK = env("FYERS_NEWTOKEN_MARK", ".token/new")      # exists only when THIS run created a new login (the workflow then caches it)
LOGIN_WAIT_MIN = int(env("LOGIN_WAIT_MIN", "15"))             # minutes the morning run waits for the code you send on Telegram
SCAN_LOGIN_WAIT_MIN = int(env("SCAN_LOGIN_WAIT_MIN", "5"))
REFRESH_DAYS = 15                                         # a FYERS refresh token lasts 15 days
HIST_DAYS = int(env("HIST_DAYS", "45"))                    # calendar days of 15m candles (about 30 trading days)
SESSION_START, SESSION_END = 9 * 60 + 15, 15 * 60 + 30    # minutes of the day, IST
LAST_ENTRY = env("LAST_ENTRY_IST", "15:30")               # BUY alerts are sent for readings known up to this time (15:30 = the whole session)
REMIND_AT = env("EOD_NOTE_IST", "15:15")                # one note per day listing the positions I am still tracking
MAX_AGE_MIN = int(env("MAX_SIGNAL_AGE_MIN", "100"))      # a BUY reading older than this when we see it is not alerted
TG_TOKEN, TG_CHAT_ID = FD.TG_TOKEN, FD.TG_CHAT_ID
MIN_BARS = 150
PROBE_MAX_SEC = int(env("PROBE_MAX_SEC", "90"))           # how long a run waits for FYERS to publish the candle that just closed
EMA_LEN = int(env("EMA_LEN", "5"))


def hm(text):
    h, m = text.split(":")
    return int(h) * 60 + int(m)


# ----------------------------------- candles -----------------------------------
def completed_15m(candles, now_ts):
    seen = {}
    for c in candles:
        seen[int(c[0])] = c
    out = []
    for t in sorted(seen):
        if t + 900 > now_ts:
            continue                                      # still forming
        c = seen[t]
        out.append((t, float(c[1]), float(c[2]), float(c[3]), float(c[4])))
    return out


def aggregate(c15, length_min):
    """Session-aligned candles built from 15m candles. A candle is kept only if ALL its 15m parts exist."""
    groups = {}
    for (t, o, h, l, c) in c15:
        dt = datetime.fromtimestamp(t, IST)
        m = dt.hour * 60 + dt.minute
        if m < SESSION_START or m >= SESSION_END:
            continue
        slot = (m - SESSION_START) // length_min
        groups.setdefault((dt.date(), slot), []).append((t, o, h, l, c))
    bars = []
    for (d, slot), g in sorted(groups.items()):
        g.sort()
        start_m = SESSION_START + slot * length_min
        end_m = min(start_m + length_min, SESSION_END)
        if len(g) != (end_m - start_m) // 15:
            continue
        first = datetime.fromtimestamp(g[0][0], IST)
        if first.hour * 60 + first.minute != start_m:
            continue
        bars.append({"t": g[0][0], "end": g[0][0] + (end_m - start_m) * 60, "date": d, "start_m": start_m, "end_m": end_m,
                     "o": g[0][1], "h": max(x[2] for x in g), "l": min(x[3] for x in g), "c": g[-1][4]})
    return bars


def readings(c15):
    """One reading per finished 1h candle: (1h candle, 45m candle read, tsi, signal, diff) in time order."""
    b45, b60 = aggregate(c15, 45), aggregate(c15, 60)
    if len(b45) < MIN_BARS:
        return []
    tsi, sig = FD.tsi_series([b["c"] for b in b45])
    index = {(b["date"], b["start_m"]): k for k, b in enumerate(b45)}
    out = []
    for hb in b60:
        k45 = (hb["end_m"] - SESSION_START - 1) // 45          # last 45m candle that OPENED before this 1h candle closed
        j = index.get((hb["date"], SESSION_START + k45 * 45))
        if j is None:
            continue                                            # that 45m candle is not finished yet
        out.append({"h": hb, "b": b45[j], "tsi": tsi[j], "sig": sig[j], "diff": tsi[j] - sig[j]})
    return out


def crossovers(rs):
    """Crossover events between consecutive readings: [(direction, reading)]."""
    ev = []
    for prev, cur in zip(rs, rs[1:]):
        if prev["diff"] <= 0 < cur["diff"]:
            ev.append((1, cur))
        elif prev["diff"] >= 0 > cur["diff"]:
            ev.append((-1, cur))
    return ev


def ema_signals(c15):
    """2h EMA(5) of closes vs every finished 1h candle close.
    A 1h close is compared with the EMA of the last 2h candle that had already closed before that 1h candle's
    close (same side as comparing with the still-forming 2h EMA). Returns {"events": [...], "last": {...}}.
    Event: {"dir": +1 close above / -1 close below, "h": 1h candle, "close", "ema", "tsi_state": +1/-1/0, "known"}."""
    b45, b60, b120 = aggregate(c15, 45), aggregate(c15, 60), aggregate(c15, 120)
    if len(b120) < EMA_LEN + 3 or len(b45) < MIN_BARS:
        return {"events": [], "last": None}
    e2 = FD.ema([b["c"] for b in b120], EMA_LEN)
    ends2 = [b["end"] for b in b120]
    tsi, sig = FD.tsi_series([b["c"] for b in b45])
    ends45 = [b["end"] for b in b45]
    rows = []
    for hb in b60:
        j = bisect.bisect_right(ends2, hb["t"]) - 1            # last 2h candle finished at or before this 1h candle OPENED
        if j < EMA_LEN:
            continue
        k = bisect.bisect_right(ends45, hb["end"]) - 1          # latest 45m candle finished when this 1h candle closed
        state = 0
        if k >= 0:
            d = tsi[k] - sig[k]
            state = 1 if d > 0 else -1 if d < 0 else 0
        side = 1 if hb["c"] > e2[j] else -1 if hb["c"] < e2[j] else 0
        rows.append({"h": hb, "close": hb["c"], "ema": e2[j], "side": side, "state": state, "known": hb["end"]})
    events = []
    for prev, cur in zip(rows, rows[1:]):
        if prev["side"] <= 0 < cur["side"] or prev["side"] >= 0 > cur["side"]:
            if cur["side"] != 0:
                events.append({"dir": cur["side"], **{k: cur[k] for k in ("h", "close", "ema", "state", "known")}})
    return {"events": events, "last": rows[-1] if rows else None}


def alert_events(es):
    """The events you asked for: TSI bullish + close below the EMA, TSI bearish + close above the EMA."""
    return [e for e in es["events"] if (e["state"] == 1 and e["dir"] == -1) or (e["state"] == -1 and e["dir"] == 1)]


# ----------------------------------- state -----------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)


def load_token(today, now=None):
    """Today's saved login. A FYERS login dies at 6:30 AM, so one made at 00:34 is already dead by 9:30."""
    try:
        with open(TOKEN_FILE) as f:
            j = json.load(f)
        if j.get("day") == today and j.get("token"):
            if now is not None and j.get("ts"):
                made = datetime.fromtimestamp(j["ts"], IST)
                dies = made.replace(hour=6, minute=30, second=0, microsecond=0)
                if dies <= made:
                    dies += timedelta(days=1)
                if now >= dies:
                    return None
            return j["token"]
    except Exception:
        pass
    return None


# ----------------------------------- telegram: buttons and messages -----------------------------------
def send_buttons(text, rows):
    """Send a message with tappable buttons. rows = [[(label, callback_data), ...], ...]"""
    print(text, flush=True)
    payload = {"chat_id": TG_CHAT_ID, "text": text[:3900],
               "reply_markup": {"inline_keyboard": [[{"text": a, "callback_data": b} for a, b in row] for row in rows]}}
    try:
        r = requests.post("https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN, json=payload, timeout=20)
        if not r.json().get("ok"):
            print("Telegram error:", r.text, flush=True)
            send(text)                                           # fall back to a plain message
    except Exception as e:
        print("Telegram send failed:", e, flush=True)
        send(text)


def out_buttons(sym):
    return [[("✅ I am out of %s" % sym, "out:" + sym), ("🔄 Still holding", "hold:" + sym)]]


# ----------------------------------- telegram: what you send -----------------------------------
WORDS = {"list", "/list", "status", "/status", "help", "/help", "/start", "start", "hi", "hello", "ok", "thanks"}


def norm(sym):
    sym = sym.strip().upper()
    if sym.startswith("NSE:"):
        sym = sym[4:]
    return sym[:-3] if sym.endswith("-EQ") else sym


def fyers_symbol(sym):
    return "NSE:%s-EQ" % sym


def poll_commands(st, now_ts):
    """Read what you sent the bot since last time. A plain stock name = "I bought it, track it".
    Returns the replies as [(text, buttons or None)]."""
    replies = []
    try:
        r = requests.get("https://api.telegram.org/bot%s/getUpdates" % TG_TOKEN,
                         params={"offset": st.get("tg_offset", 0), "timeout": 0}, timeout=20).json()
    except Exception as e:
        print("Telegram read failed:", e, flush=True)
        return replies
    if not r.get("ok"):
        print("Telegram read problem:", r, flush=True)
        return replies
    holdings = st.setdefault("holdings", {})

    def track(sym, when):
        if sym in holdings:
            replies.append(("You are already tracked in %s." % sym, out_buttons(sym)))
        else:
            holdings[sym] = {"since": when, "entry_ts": None}      # the entry is worked out from its bullish crossover in the scan

    def untrack(sym):
        h = holdings.pop(sym, None)
        replies.append(("👍 Noted - you are out of %s. I stopped tracking it." % sym if h else "I was not tracking %s." % sym, None))
        if h:
            st.setdefault("closed", []).append({"sym": sym, "entry_price": h.get("entry_price"), "entry_label": h.get("entry_label")})

    for u in r.get("result", []):
        st["tg_offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if cq:                                                   # you pressed a button
            chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
            if str(chat) != str(TG_CHAT_ID):
                continue
            try:
                requests.post("https://api.telegram.org/bot%s/answerCallbackQuery" % TG_TOKEN, data={"callback_query_id": cq["id"]}, timeout=10)
            except Exception:
                pass
            action, _, sym = (cq.get("data") or "").partition(":")
            if action == "out":
                untrack(sym)
            elif action == "hold":
                replies.append(("🔄 OK, still holding %s - I keep watching and will warn you on the next bearish crossover." % sym, None))
            elif action == "in":
                track(sym, int((cq.get("message") or {}).get("date") or now_ts))
            continue
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != str(TG_CHAT_ID):
            continue
        text = (m.get("text") or "").strip()
        when = int(m.get("date") or now_ts)
        low = text.lower()
        if looks_like_code(text):
            tg_delete(m.get("message_id"))
            try:
                cmd_login(text, datetime.now(IST))
            except FD.FyersError as e:
                replies.append(("❌ That code did not work (%s)." % e, None))
            continue
        mo = re.match(r"^/?(out|sell|sold|exit|close)(?:@\w+)?\s+([A-Za-z0-9&\-\.:]+)(?:\s+[0-9.]+)?\s*$", text, re.I)
        mi = re.match(r"^/?(in|buy|bought|long)(?:@\w+)?\s+([A-Za-z0-9&\-\.:]+)(?:\s+[0-9.]+)?\s*$", text, re.I)   # the old long form still works
        if mo:
            untrack(norm(mo.group(2)))
        elif mi:
            track(norm(mi.group(2)), when)
        elif low in ("list", "/list", "status", "/status"):
            if holdings:
                replies.append(("Tracking your position(s):\n" + "\n".join("%s%s" % (s, " from %s at about %.2f" % (h["entry_label"], h["entry_price"])
                                                                                  if h.get("entry_ts") else " (working out the entry)") for s, h in sorted(holdings.items())), None))
            else:
                replies.append(("No positions tracked. Just send me a stock name, for example: RELIANCE", None))
        elif low in WORDS:
            replies.append(("Just send me the stock name when you buy, for example: RELIANCE\nI track it from its latest bullish signal, "
                            "send you its performance every morning, and warn you when the 1h TSI turns bearish.\n"
                            "Other messages: list | out RELIANCE", None))
        elif re.match(r"^[A-Za-z0-9&\-\.]{1,20}$", text):
            track(norm(text), when)
        elif text:
            replies.append(("I did not understand \"%s\". Send a stock name, for example: RELIANCE" % text[:40], None))
    return replies


# ----------------------------------- scan -----------------------------------
def fmt_time(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M")


def fmt_when(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%d %b %H:%M")


def entry_from(rs, c15, es):
    """Entry = the latest BULLISH signal: a bullish TSI crossover or a 1h close above the 2h 5 EMA (whichever is newer).
    Returns (entry_ts, price, label, warning)."""
    ev = crossovers(rs)
    cands = [(e[1]["b"]["end"], e[1]["b"]["c"], "bullish TSI crossover, %s" % fmt_when(e[1]["b"]["end"])) for e in ev if e[0] == 1]
    cands += [(e["known"], e["close"], "1h close above the 2h 5 EMA, %s" % fmt_when(e["known"])) for e in es["events"] if e["dir"] == 1]
    if not cands:
        px = c15[-1][4]
        return int(c15[-1][0] + 900), px, "now (no bullish signal in the last %d days)" % HIST_DAYS, ""
    ts, px, label = max(cands, key=lambda x: x[0])
    warn = ""
    if ev and ev[-1][0] == -1 and ev[-1][1]["b"]["end"] > ts:
        warn = "\n⚠️ The 1h TSI already turned BEARISH on %s - you may be late." % fmt_when(ev[-1][1]["b"]["end"])
    return ts, px, label, warn


def ema_text(es):
    last = es["last"] if es else None
    if not last:
        return ""
    return "\n   2h 5 EMA %.2f: last 1h close %.2f is %s it" % (last["ema"], last["close"], "ABOVE" if last["side"] > 0 else "BELOW" if last["side"] < 0 else "on")


def performance_line(sym, h, c15, rs, es=None):
    px = c15[-1][4]
    since = [c for c in c15 if c[0] >= h["entry_ts"] - 900]
    hi = max([c[2] for c in since] or [px])
    lo = min([c[3] for c in since] or [px])
    days = len({datetime.fromtimestamp(c[0], IST).date() for c in since}) or 1
    e = h["entry_price"]
    state = "still bullish" if rs and rs[-1]["diff"] > 0 else "BEARISH now"
    tsi = "TSI %.1f vs signal %.1f (%s)" % (rs[-1]["tsi"], rs[-1]["sig"], state) if rs else ""
    return ("%s - since the entry signal (%s, about %.2f): now %.2f (%+.2f%%) | trading day %d\n"
            "   highest since entry %.2f (%+.2f%%), lowest %.2f (%+.2f%%)\n   %s%s" % (
                sym, h["entry_label"], e, px, (px / e - 1) * 100, days, hi, (hi / e - 1) * 100, lo, (lo / e - 1) * 100, tsi, ema_text(es)))


def wait_for_candle(token, sym, now):
    """Runs start right on a quarter-hour. FYERS may need a few seconds to publish the 15m candle that just closed, so
    wait (checking one stock every 8 s, at most PROBE_MAX_SEC) until it is there."""
    now_ts = int(now.timestamp())
    exp_start = (now_ts // 900) * 900 - 900                     # IST is a whole number of quarter-hours from UTC
    d = datetime.fromtimestamp(exp_start, IST)
    if d.date() != now.date() or not (SESSION_START <= d.hour * 60 + d.minute <= SESSION_END - 15):
        return True
    waited = 0
    while True:
        try:
            if any(int(c[0]) == exp_start for c in FD.history(token, fyers_symbol(sym), resolution="15", days=3)):
                if waited:
                    print("candle %s appeared after %d s" % (fmt_time(exp_start), waited), flush=True)
                return True
        except FD.RateLimited:
            return False
        except FD.FyersError as e:
            print("probe problem:", e, flush=True)
        if waited >= PROBE_MAX_SEC:
            print("candle %s not published after %d s - scanning anyway" % (fmt_time(exp_start), waited), flush=True)
            return False
        time.sleep(8)
        waited += 8


def scan(token, st, now):
    now_ts = int(now.timestamp())
    today = now.strftime("%Y-%m-%d")
    minute = now.hour * 60 + now.minute
    holdings = st.setdefault("holdings", {})
    out = []                                                     # [(text, buttons)]

    morning = st.get("day") != today                             # first run of a new day
    if morning:
        st.update(day=today, reminded=False, summary=False)

    out += poll_commands(st, now_ts)

    # before the first reading of the day (10:45) nothing new can exist: only look at your positions
    syms = sorted(set(holdings)) if minute < hm("10:40") else sorted(set(FD.short(s) for s in FD.symbols()) | set(holdings))
    watermarks = st.setdefault("watermark", {})
    if syms:
        wait_for_candle(token, syms[0], now)
    buys, emas, exits, errors, scanned, stopped = [], [], [], [], 0, None
    seen = {}
    for n, sym in enumerate(syms):
        if n:
            time.sleep(FD.PACE_SECONDS)
        try:
            candles = FD.history(token, fyers_symbol(sym), resolution="15", days=HIST_DAYS)
        except FD.RateLimited as e:
            stopped = "%s (stopped after %d of %d stocks)" % (e, n, len(syms))
            break
        except FD.FyersError as e:
            errors.append("%s: %s" % (sym, e))
            if sym in holdings and not holdings[sym].get("entry_ts"):
                holdings.pop(sym)
                out.append(("⚠️ I could not read %s from FYERS (%s), so I am not tracking it. Check the stock name." % (sym, e), None))
            continue
        c15 = completed_15m(candles, now_ts)
        rs = readings(c15)
        if not rs:
            errors.append("%s: not enough candles" % sym)
            continue
        scanned += 1
        es = ema_signals(c15)
        seen[sym] = (c15, rs, es)
        mark = watermarks.get(sym)
        watermarks[sym] = min(now_ts, c15[-1][0] + 900)         # never move past data FYERS has not published yet
        h = holdings.get(sym)
        if h is not None and not h.get("entry_ts"):             # a stock you just told me about: find its bullish crossover
            h["entry_ts"], h["entry_price"], h["entry_label"], warn = entry_from(rs, c15, es)
            px = c15[-1][4]
            out.append(("✅ Tracking %s from its latest bullish signal (%s, about %.2f). Now about %.2f (%+.2f%%).%s\n"
                        "I will send its performance every morning and warn you when the 1h TSI turns bearish or the EMA alerts fire." % (
                            sym, h["entry_label"], h["entry_price"], px, (px / h["entry_price"] - 1) * 100, warn), out_buttons(sym)))
        if mark is None:
            continue                                            # first time we see this stock: start from now, no old alerts
        for d, rd in crossovers(rs):
            known = rd["b"]["end"]
            if not (mark < known <= now_ts):
                continue
            kt = datetime.fromtimestamp(known, IST)
            if d == 1:
                if kt.hour * 60 + kt.minute > hm(LAST_ENTRY) or now_ts - known > MAX_AGE_MIN * 60:
                    continue
                buys.append((sym, rd, known))
            elif h is not None and h.get("entry_ts") and known > h["entry_ts"]:
                exits.append((sym, rd, known))
        for e in alert_events(es):
            if mark < e["known"] <= now_ts and now_ts - e["known"] <= MAX_AGE_MIN * 60 \
                    and datetime.fromtimestamp(e["known"], IST).hour * 60 + datetime.fromtimestamp(e["known"], IST).minute <= hm(LAST_ENTRY):
                emas.append((sym, e))

    for sym, rd, known in exits:
        h, (c15, rs, es_) = holdings[sym], seen[sym]
        px = c15[-1][4]
        out.append(("🔴 TIME TO EXIT %s - the 1h TSI turned BEARISH.\n1h candle %s-%s, confirmed %s IST | now about %.2f | TSI %.1f vs signal %.1f\n"
                    "Since the entry signal (%s, about %.2f): %+.2f%%.\nWhen you have sold, press the button." % (
                        sym, fmt_time(rd["h"]["t"]), fmt_time(rd["h"]["end"]), fmt_time(known), px, rd["tsi"], rd["sig"],
                        h["entry_label"], h["entry_price"], (px / h["entry_price"] - 1) * 100), out_buttons(sym)))
    if buys or emas:
        parts, rows_, seen_b = [], [], set()
        if buys:
            parts.append("🟢 BULLISH 1h TSI crossover (%d):\n%s" % (len(buys), "\n".join(
                "%s  about %.2f | 1h candle %s-%s, confirmed %s | TSI %.1f vs signal %.1f" % (
                    sym, rd["b"]["c"], fmt_time(rd["h"]["t"]), fmt_time(rd["h"]["end"]), fmt_time(known), rd["tsi"], rd["sig"])
                for sym, rd, known in sorted(buys, key=lambda x: x[0]))))
        if emas:
            lines = []
            for sym, e in sorted(emas, key=lambda x: x[0]):
                if e["dir"] == -1:
                    what = "closed BELOW the 2h 5 EMA while the TSI is bullish"
                else:
                    what = "closed ABOVE the 2h 5 EMA while the TSI is bearish"
                lines.append("%s  1h candle %s-%s %s: close %.2f vs EMA %.2f%s" % (
                    sym, fmt_time(e["h"]["t"]), fmt_time(e["h"]["end"]), what, e["close"], e["ema"], "  (you are tracking this)" if sym in holdings else ""))
            parts.append("🔔 2h 5 EMA alert (%d):\n%s" % (len(emas), "\n".join(lines)))
        for sym in sorted({x[0] for x in buys} | {x[0] for x in emas}):
            if sym in seen_b:
                continue
            seen_b.add(sym)
            rows_.append([("✅ I am out of %s" % sym, "out:" + sym), ("🔄 Still holding", "hold:" + sym)] if sym in holdings
                         else [("📌 I bought %s" % sym, "in:" + sym)])
        foot = "\n\nIf you buy, press the button (or just send me the stock name)." if any(r[0][1].startswith("in:") for r in rows_) else \
            "\n\nPress I am out when you have sold, or Still holding to keep watching."
        out.append(("\n\n".join(parts) + foot, rows_))

    if morning and holdings:                                     # every morning until you press Out
        lines = [performance_line(s, h, *seen[s]) for s, h in sorted(holdings.items()) if h.get("entry_ts") and s in seen]
        if lines:
            out.append(("☀️ Morning update - %s (price as of the last finished 15-minute candle):\n\n%s\n\nPress a button when you are out." % (
                now.strftime("%d %b"), "\n\n".join(lines)), [[("✅ I am out of %s" % s, "out:" + s)] for s in sorted(holdings) if s in seen and holdings[s].get("entry_ts")]))
    if holdings and not st.get("reminded") and minute >= hm(REMIND_AT):
        out.append(("ℹ️ Market closes at 15:30. I am still tracking: %s. Close intraday trades yourself; I keep watching the rest until you press Out." %
                    ", ".join(sorted(holdings)), None))
        st["reminded"] = True
    if minute >= hm("15:30") and not st.get("summary"):
        out.append(("✅ Scan summary %s: %d stocks scanned, %d problem(s)." % (today, scanned, len(errors)), None))
        st["summary"] = True
    if stopped:
        out.append(("⚠️ " + stopped, None))
    if errors:
        out.append(("⚠️ %d stock problem(s): %s" % (len(errors), "; ".join(errors[:6])), None))
    for text, buttons in out:
        send_buttons(text, buttons) if buttons else send(text)
    return [t for t, _ in out]


# ----------------------------------- commands -----------------------------------
def tv_diag(c15):
    """Find out which way of building the candles / source reproduces TradingView's number (default: ADANIPOWER, 1h candle
    14:15-15:15 on 16 Nov 2026 shows TSI -2.5311, signal 5.1983)."""
    target_end = datetime.strptime(env("TV_END", "2026-11-16 15:15"), "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    want_t, want_s = float(env("TV_TSI", "-2.5311")), float(env("TV_SIG", "5.1983"))
    te = int(target_end.timestamp())
    srcs = {"close": lambda b: b["c"], "open": lambda b: b["o"], "hl2": lambda b: (b["h"] + b["l"]) / 2,
            "hlc3": lambda b: (b["h"] + b["l"] + b["c"]) / 3, "ohlc4": lambda b: (b["o"] + b["h"] + b["l"] + b["c"]) / 4}
    sets = {}
    for n in (15, 30, 45, 60, 120):
        full = aggregate(c15, n)
        sets["%dm" % n] = full
        sets["%dm no short last candle" % n] = [b for b in full if b["end_m"] - b["start_m"] == n]
    sets["15m"] = [{"t": t, "end": t + 900, "o": o, "h": h, "l": l, "c": c, "start_m": 0, "end_m": 15} for (t, o, h, l, c) in c15]
    rows = []
    for name, bars in sets.items():
        for sname, f in srcs.items():
            if sname != "close" and not name.startswith(("45m", "60m")):
                continue
            idx = [k for k, b in enumerate(bars) if b["end"] == te]
            if not idx or len(bars) < 60:
                continue
            t, sg = FD.tsi_series([f(b) for b in bars])
            rows.append((abs(t[idx[0]] - want_t) + abs(sg[idx[0]] - want_s), name, sname, t[idx[0]], sg[idx[0]]))
    print("--- which setup gives TradingView's TSI %.4f / signal %.4f at %s? (closest first) ---" % (want_t, want_s, target_end.strftime("%d %b %H:%M")))
    for dist, name, sname, t, sg in sorted(rows)[:12]:
        print("%-28s source %-6s TSI %9.4f  signal %9.4f   gap %.3f" % (name, sname, t, sg, dist))
    print("--- end of search ---")


def cmd_check(token, now):
    sym = FD.short(FD.symbols()[0])
    candles = FD.history(token, fyers_symbol(sym), resolution="15", days=HIST_DAYS)
    now_ts = int(now.timestamp())
    c15 = completed_15m(candles, now_ts)
    print("%s: %d fifteen-minute candles received, %d finished." % (sym, len(candles), len(c15)))
    if candles:
        for label, c in (("first", candles[0]), ("last", candles[-1])):
            print("%s candle: epoch %s -> %s IST  O %s H %s L %s C %s" % (label, c[0], datetime.fromtimestamp(int(c[0]), IST), c[1], c[2], c[3], c[4]))
    days = {}
    for c in c15:
        d = datetime.fromtimestamp(c[0], IST)
        days.setdefault(d.date(), []).append(d.strftime("%H:%M"))
    if days:
        d = max(days)
        print("Latest day %s: first candle starts %s, last candle starts %s, %d candles (a full day has 25: 09:15 to 15:15)" % (
            d, days[d][0], days[d][-1], len(days[d])))
    b45, b60 = aggregate(c15, 45), aggregate(c15, 60)
    print("45m candles: %d | 1h candles: %d" % (len(b45), len(b60)))
    if len(b45) >= MIN_BARS:
        t45, s45 = FD.tsi_series([b["c"] for b in b45])
        print("--- every 45m candle of the last days: close, TSI, signal (compare with TradingView) ---")
        for k in range(max(0, len(b45) - 32), len(b45)):
            print("%s %s-%s  close %.2f  TSI %.4f  signal %.4f" % (datetime.fromtimestamp(b45[k]["t"], IST).strftime("%d %b"),
                  fmt_time(b45[k]["t"]), fmt_time(b45[k]["end"]), b45[k]["c"], t45[k], s45[k]))
        print("--- end of table; %d 45m candles were used for the calculation ---" % len(b45))
    tv_diag(c15)
    rs = readings(c15)
    for rd in rs[-6:]:
        print("1h %s-%s reads 45m %s-%s (known %s): TSI %.2f signal %.2f diff %+.2f" % (
            fmt_time(rd["h"]["t"]), fmt_time(rd["h"]["end"]), fmt_time(rd["b"]["t"]), fmt_time(rd["b"]["end"]),
            fmt_time(rd["b"]["end"]), rd["tsi"], rd["sig"], rd["diff"]))
    for d, rd in crossovers(rs)[-3:]:
        print("Recent crossover: %s on 1h candle %s %s-%s" % ("BULLISH" if d == 1 else "BEARISH",
                                                            datetime.fromtimestamp(rd["h"]["t"], IST).date(), fmt_time(rd["h"]["t"]), fmt_time(rd["h"]["end"])))
    es = ema_signals(c15)
    if es["last"]:
        l = es["last"]
        print("2h 5 EMA %.2f | last finished 1h candle %s-%s close %.2f is %s the EMA | TSI state %s" % (
            l["ema"], fmt_time(l["h"]["t"]), fmt_time(l["h"]["end"]), l["close"], "above" if l["side"] > 0 else "below",
            "bullish" if l["state"] > 0 else "bearish"))
    for e in es["events"][-3:]:
        print("Recent EMA cross: 1h close %s the 2h EMA on %s %s-%s (TSI %s) -> %s" % (
            "ABOVE" if e["dir"] > 0 else "BELOW", datetime.fromtimestamp(e["h"]["t"], IST).date(), fmt_time(e["h"]["t"]), fmt_time(e["h"]["end"]),
            "bullish" if e["state"] > 0 else "bearish", "ALERT" if e in alert_events(es) else "no alert"))
    print("Check finished: no orders exist in this program.")


def looks_like_code(text):
    t = (text or "").strip()
    return "auth_code=" in t or (len(t) >= 80 and " " not in t)


def tg_delete(message_id):
    try:
        requests.post("https://api.telegram.org/bot%s/deleteMessage" % TG_TOKEN, data={"chat_id": TG_CHAT_ID, "message_id": message_id}, timeout=10)
    except Exception:
        pass


def login_prompt(minutes):
    send("🔑 FYERS login needed today.\n\n1) Press and hold this link, choose Copy, open it in Chrome and log in:\n%s\n\n"
         "2) You land on a page showing the authorisation code. Press and hold the long code, select all, copy it.\n"
         "3) SEND THE CODE TO ME HERE in this chat. Do it quickly - the code lives only a minute or two.\n\n"
         "I am listening for %d minutes." % (FD.login_link(), minutes))


def wait_for_login(st, now, minutes):
    """Listen on Telegram for the code you send. Returns True when the login worked."""
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        try:
            r = requests.get("https://api.telegram.org/bot%s/getUpdates" % TG_TOKEN,
                             params={"offset": st.get("tg_offset", 0), "timeout": 15}, timeout=30).json()
        except Exception as e:
            print("Telegram read failed:", e, flush=True)
            time.sleep(3)
            continue
        for u in r.get("result", []) if r.get("ok") else []:
            m = u.get("message") or {}
            if str((m.get("chat") or {}).get("id")) != str(TG_CHAT_ID):
                st["tg_offset"] = u["update_id"] + 1
                continue
            text = (m.get("text") or "").strip()
            if not looks_like_code(text):
                continue                                         # keep other messages for the normal scan
            st["tg_offset"] = u["update_id"] + 1
            tg_delete(m.get("message_id"))
            try:
                cmd_login(text, datetime.now(IST))
                return True
            except FD.FyersError as e:
                send("❌ That code did not work (%s). Tap the link again and send me a fresh code." % e)
        time.sleep(2)
    send("⏰ I did not get a login code in %d minutes. Run login-link in GitHub or wait for the next prompt." % minutes)
    return False


def save_token(token, now):
    os.makedirs(os.path.dirname(TOKEN_FILE) or ".", exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump({"day": now.strftime("%Y-%m-%d"), "token": token, "ts": int(now.timestamp())}, f)
    with open(NEW_TOKEN_MARK, "w") as f:
        f.write("new")


def save_refresh(refresh, now):
    os.makedirs(os.path.dirname(REFRESH_FILE) or ".", exist_ok=True)
    with open(REFRESH_FILE, "w") as f:
        json.dump({"refresh_token": refresh, "issued": now.strftime("%Y-%m-%d")}, f)


def cmd_login(code, now):
    token = FD.get_token(code)
    save_token(token, now)
    extra = ""
    if FD.LAST_REFRESH:
        save_refresh(FD.LAST_REFRESH, now)
        extra = " Automatic daily renewal is armed for the next %d days." % REFRESH_DAYS
    send("✅ FYERS login OK for %s. The stock scans can run now.%s" % (now.strftime("%d %b %Y"), extra))


def try_refresh(now):
    """Every morning: get today's token without you (FYERS_PIN secret + the refresh token of your last login).
    Returns "" when it worked, otherwise the reason it did not."""
    pin = env("FYERS_PIN")
    if not pin:
        return "no FYERS_PIN secret is set"
    try:
        with open(REFRESH_FILE) as f:
            r = json.load(f)
        refresh, issued = r["refresh_token"], r.get("issued", "")
    except Exception:
        return "no refresh token saved yet (this is normal before your first login)"
    try:
        token, new_refresh = FD.refresh_access(refresh, pin)
    except FD.FyersError as e:
        return str(e)
    save_token(token, now)
    if new_refresh:
        save_refresh(new_refresh, now)
        issued = now.strftime("%Y-%m-%d")
    try:
        left = REFRESH_DAYS - (now.date() - datetime.strptime(issued, "%Y-%m-%d").date()).days
    except Exception:
        left = None
    note = ""
    if left is not None:
        note = " Manual login is needed again in about %d day(s)." % max(left, 0) if left <= 4 else " (%d days of automatic renewal left)" % left
    send("✅ FYERS auto-login OK for %s.%s" % (now.strftime("%d %b %Y"), note))
    return ""


def telegram_login(st, now, minutes, why=""):
    if why:
        print("Automatic renewal did not work:", why, flush=True)
    login_prompt(minutes)
    return wait_for_login(st, now, minutes)


def main():
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    try:
        os.remove(NEW_TOKEN_MARK)                                # set again only if this run creates a login
    except OSError:
        pass
    if "--login" in sys.argv:
        cmd_login(env("AUTH_CODE"), now)
        return
    st = load_state()
    try:
        if "--login-link" in sys.argv:
            telegram_login(st, now, LOGIN_WAIT_MIN)
            return
        if "--refresh" in sys.argv:
            if load_token(today, now) is None:
                why = try_refresh(now)
                if why:
                    telegram_login(st, now, LOGIN_WAIT_MIN, why)
            return
        token = load_token(today, now)
        if not token:
            if st.get("login_alert") == today:
                print("No FYERS token for today.")
                return
            st["login_alert"] = today
            why = try_refresh(now)                               # last chance to log in by itself
            if why and not telegram_login(st, now, SCAN_LOGIN_WAIT_MIN, why):
                return
            token = load_token(today, now)
            if not token:
                return
        if "--check" in sys.argv:
            cmd_check(token, now)
        elif "--scan" in sys.argv:
            scan(token, st, now)
        else:
            raise SystemExit("Use --login-link, --login, --refresh, --check or --scan.")
    finally:
        save_state(st)


if __name__ == "__main__":
    try:
        main()
    except (FD.FyersError, SystemExit) as e:
        if isinstance(e, SystemExit) and not e.code:
            raise
        send("❌ Stock TSI problem: %s" % (e.code if isinstance(e, SystemExit) else e))
        raise SystemExit(1)
