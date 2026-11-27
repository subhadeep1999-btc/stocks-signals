"""
SYSTEM 2 - 5 EMA (calculated on 30-minute candles) read on the 15-minute chart.  Signals only: this program never places an order.

  * Same stocks as System 1 (stocks.txt). 15-minute candles. One indicator: EMA 5 on 30-minute candles.
    The EMA used for a 15m candle is the one of the last 30m candle that was FINISHED before that 15m candle opened.
  * BUY signal : a bullish 15m candle CLOSES above the EMA and its low touches or is below the EMA (body or wick).
    SELL signal: a bearish 15m candle CLOSES below the EMA and its high touches or is above the EMA.
    A candle that is entirely above/below the EMA (a gap open) gives no signal.
  * One message per run: all BULLISH (green) signals together, all BEARISH (red) signals together, a chart picture per signal,
    and the buttons below the message. Tap "BUY X" / "SELL X" when you take the trade (manual for now).
  * After the entry the bot asks for the stop loss: 13 points (default) or 0.35% / 0.5% / 0.75% / 0.85% of the entry price,
    each shown in points. 1R = the stop-loss distance in points.
  * Alerts: 1:1, 1:2, 1:3 ... (price reached entry +/- n x R). Exit: a candle CLOSES on the other side of the EMA (no reversal),
    or the stop loss is touched, or - once the trade had reached 1:5 - a fall back to 1:3 (profit lock, book 1:3).
  * At most MAX_OPEN trades at once (the fund is split in half when automatic trading is added later).
"""
import json
import math
import os
import re
import time
from bisect import bisect_right
from datetime import datetime, timedelta

import requests

import fyers_daily as FD
from fyers_daily import IST, env

EMA_LEN = int(env("S2_EMA_LEN", "5"))
SL_PCTS = [0.35, 0.5, 0.75, 0.85]
DEFAULT_SL_PTS = float(env("S2_DEFAULT_SL_PTS", "13"))
LOCK_START_R = int(env("S2_LOCK_START_R", "5"))          # once the trade has reached this many R ...
LOCK_AT_R = int(env("S2_LOCK_AT_R", "3"))                # ... a fall back to this many R means: book the profit
LAST_ENTRY_MIN = 14 * 60 + 45                            # no new entries from a candle that closes after 14:45
NOTE_AT_MIN = 15 * 60 + 10                               # one reminder for open trades
MAX_OPEN = int(env("S2_MAX_OPEN", "2"))
SIGNAL_MAX_AGE_MIN = int(env("S2_SIGNAL_MAX_AGE_MIN", "20"))
HIST_DAYS = int(env("S2_HIST_DAYS", "8"))
PACE = float(env("S2_PACE_SECONDS", "0.7"))
CHARTS_MAX = int(env("S2_CHARTS_MAX", "6"))
SESSION_START, SESSION_END = 9 * 60 + 15, 15 * 60 + 30

# set by tsi_stocks.py (keeps this file free of imports from it)
NORM = lambda s: s.strip().upper()
SEND_BUTTONS = None
WAIT_FOR_CANDLE = None
SEND = FD.send if hasattr(FD, "send") else None


def fyers_symbol(sym):
    return "NSE:%s-EQ" % sym


def fmt_time(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%H:%M")


def mins(ts):
    d = datetime.fromtimestamp(ts, IST)
    return d.hour * 60 + d.minute


# ----------------------------------- candles and EMA -----------------------------------
def to_candles(raw, now_ts):
    """[(t, o, h, l, c, done)] oldest first; done = the 15m candle has finished."""
    seen = {}
    for c in raw:
        seen[int(c[0])] = c
    out = []
    for t in sorted(seen):
        c = seen[t]
        out.append((t, float(c[1]), float(c[2]), float(c[3]), float(c[4]), t + 900 <= now_ts))
    return out


def bars30(c15):
    """30-minute candles from the FINISHED 15m candles, aligned to 9:15 (the last one, 15:15-15:30, is a single 15m candle)."""
    groups = {}
    full = (SESSION_END - SESSION_START) // 30
    for (t, o, h, l, c, done) in c15:
        if not done:
            continue
        m = mins(t)
        if m < SESSION_START or m >= SESSION_END:
            continue
        slot = (m - SESSION_START) // 30
        groups.setdefault((datetime.fromtimestamp(t, IST).date(), slot), []).append((t, o, h, l, c))
    bars = []
    for (d, slot), g in sorted(groups.items()):
        g.sort()
        need = 2 if slot < full else 1
        if len(g) != need:
            continue
        start_m = SESSION_START + slot * 30
        if mins(g[0][0]) != start_m:
            continue
        bars.append({"t": g[0][0], "end": g[0][0] + len(g) * 900, "c": g[-1][4]})
    return bars


def ema_lookup(c15):
    """Returns f(t) = EMA 5 of the 30m candles that were finished at or before time t (None if there are too few)."""
    bars = bars30(c15)
    if len(bars) < EMA_LEN + 2:
        return lambda t: None
    ev = FD.ema([b["c"] for b in bars], EMA_LEN)
    ends = [b["end"] for b in bars]

    def at(t):
        i = bisect_right(ends, t) - 1
        return ev[i] if i >= 0 else None
    return at


# ----------------------------------- signals -----------------------------------
def candle_signal(o, h, l, c, e):
    """1 = buy signal, -1 = sell signal, 0 = none."""
    if e is None:
        return 0
    if c > o and l <= e < c:
        return 1
    if c < o and h >= e > c:
        return -1
    return 0


def find_signals(sym, c15, now_ts, at=None):
    """Signals on candles that finished within SIGNAL_MAX_AGE_MIN minutes."""
    at = at or ema_lookup(c15)
    out = []
    for (t, o, h, l, c, done) in c15:
        close_ts = t + 900
        if not done or now_ts - close_ts > SIGNAL_MAX_AGE_MIN * 60:
            continue
        m = mins(t)
        if m < SESSION_START or m >= SESSION_END:
            continue
        e = at(t)
        d = candle_signal(o, h, l, c, e)
        if d:
            out.append({"sym": sym, "dir": d, "t": t, "close_ts": close_ts, "o": o, "h": h, "l": l, "c": c, "ema": e,
                        "late": mins(close_ts) > LAST_ENTRY_MIN})
    return out


# ----------------------------------- trades -----------------------------------
def s2(st):
    return st.setdefault("s2", {"pending": {}, "trades": {}, "seen": [], "closed": [], "note_day": ""})


def r_of(trade):
    return trade["sl"]


def stop_price(trade):
    return trade["entry"] - trade["dir"] * trade["sl"]


def level_price(trade, n):
    return trade["entry"] + trade["dir"] * n * trade["sl"]


def levels_text(trade):
    return " | ".join("1:%d %.2f" % (n, level_price(trade, n)) for n in (1, 2, 3, 4, 5))


def evaluate(trade, c15, at):
    """Walks the candles since the entry and returns (levels_reached, exit or None). Pure function of the data, so it can be re-run every time.
    Inside one candle the order of high and low is unknown, so the stop loss is checked first (the cautious choice)."""
    d, E, R = trade["dir"], trade["entry"], trade["sl"]
    stop = E - d * R
    lock_px = E + d * LOCK_AT_R * R
    best, armed, levels = E, False, 0
    for (t, o, h, l, c, done) in c15:
        if t < trade["ts"]:
            continue
        fav, adv = (h, l) if d == 1 else (l, h)
        if armed and d * (adv - lock_px) <= 0:                    # falling back, price meets the 1:3 level before it can reach the stop
            return levels, {"kind": "lock", "price": lock_px, "t": t}
        if d * (adv - stop) <= 0:
            return levels, {"kind": "stop", "price": stop, "t": t}
        if d * (fav - best) > 0:
            best = fav
        levels = max(levels, int(math.floor(d * (best - E) / R + 1e-9)))
        if levels >= LOCK_START_R:
            armed = True                                          # takes effect from the NEXT candle
        if done:
            e = at(t)
            if e is not None and d * (c - e) < 0:
                return levels, {"kind": "ema", "price": c, "t": t, "ema": e}
    return levels, None


def result_r(trade, price):
    return trade["dir"] * (price - trade["entry"]) / trade["sl"]


def word(d):
    return "BUY" if d == 1 else "SELL"


def mark(d):
    return "🟢" if d == 1 else "🔴"


def sl_menu(trade, sym):
    """The stop-loss choices (message text lines and the keyboard rows)."""
    E = trade["entry"]
    lines, keys = [], []
    opts = [("%g pts (default)" % DEFAULT_SL_PTS, DEFAULT_SL_PTS, "SL %s %g pts" % (sym, DEFAULT_SL_PTS))]
    for p in SL_PCTS:
        pts = round(E * p / 100.0, 2)
        opts.append(("%g%% = %.2f pts" % (p, pts), pts, "SL %s %g%% (%.1f pts)" % (sym, p, pts)))
    for label, pts, key in opts:
        px = E - trade["dir"] * pts
        lines.append("  %s -> stop %.2f%s" % (label, px, "   <- selected" if abs(pts - trade["sl"]) < 0.005 else ""))
        keys.append((key, "sl:%s" % sym))
    rows = [keys[i:i + 2] for i in range(0, len(keys), 2)]
    rows.append([("OUT %s" % sym, "out2:%s" % sym)])
    return lines, rows


def entry_message(sym, trade, how=""):
    lines, rows = sl_menu(trade, sym)
    text = ("%s %s noted - entry %.2f%s\n"
            "Stop loss now: %.2f points -> stop price %.2f\n"
            "1R = %.2f points. Targets: %s\n\n"
            "Choose another stop loss if you want (points from your entry):\n%s\n\n"
            "I will tell you at 1:1, 1:2, 1:3 ... and when to exit. If your real price was different, send: %s <price>" % (
                mark(trade["dir"]), word(trade["dir"]) + " " + sym, trade["entry"], how, trade["sl"], stop_price(trade),
                trade["sl"], levels_text(trade), "\n".join(lines), sym))
    return text, rows


# ----------------------------------- what you send -----------------------------------
def handle_text(st, text, when):
    """System 2 commands. Returns [(text, rows or None)] or None when the message is not for System 2."""
    S = s2(st)
    t = text.strip()
    low = t.lower()
    replies = []
    m = re.match(r"^(buy|sell)\s+([A-Za-z0-9&\-\.: ]+?)(?:\s+([0-9]+(?:\.[0-9]+)?))?\s*$", t, re.I)
    if m:
        d = 1 if m.group(1).lower() == "buy" else -1
        sym = NORM(m.group(2))
        typed = float(m.group(3)) if m.group(3) else None
        if sym in S["trades"]:
            tr = S["trades"][sym]
            return [("You are already in %s (%s from %.2f)." % (sym, word(tr["dir"]), tr["entry"]), None)]
        if len(S["trades"]) >= MAX_OPEN:
            return [("🚫 You already have %d open trade(s): %s. Send OUT <name> when one is closed, then try again." % (
                len(S["trades"]), ", ".join(sorted(S["trades"]))), None)]
        p = S["pending"].get(sym)
        sig_ts = None
        if typed is not None:
            entry, how = typed, " (the price you typed)"
        elif p and p["dir"] == d:
            entry, sig_ts, how = p["c"], p["close_ts"], " (close of the %s signal candle %s-%s)" % (
                "bullish" if d == 1 else "bearish", fmt_time(p["t"]), fmt_time(p["close_ts"]))
        else:
            entry = None
        if entry is None:
            S["trades"][sym] = {"dir": d, "entry": None, "ts": when, "sl": DEFAULT_SL_PTS, "announced": 0}
            return [("📌 %s %s noted. I will take the price of the last finished 15m candle at my next run and then send the stop-loss choices." % (
                word(d), sym), None)]
        tr = {"dir": d, "entry": float(entry), "ts": sig_ts or when, "sl": DEFAULT_SL_PTS, "announced": 0}
        if typed is not None:
            tr["ts"] = when
        S["trades"][sym] = tr
        S["pending"].pop(sym, None)
        txt, rows = entry_message(sym, tr, how)
        return [(txt, rows)]
    m = re.match(r"^sl\s+([A-Za-z0-9&\-\.: ]+?)\s+([0-9]+(?:\.[0-9]+)?)\s*(%?)", t, re.I)
    if m:
        sym = NORM(m.group(1))
        tr = S["trades"].get(sym)
        if not tr or tr.get("entry") is None:
            return [("I have no open System 2 trade in %s." % sym, None)]
        val = float(m.group(2))
        tr["sl"] = round(tr["entry"] * val / 100.0, 2) if m.group(3) == "%" else val
        tr["announced"] = 0                                      # levels are counted again from the new stop
        txt, rows = entry_message(sym, tr, " (stop loss changed)")
        return [(txt, rows)]
    m = re.match(r"^(?:i\s+am\s+)?(?:out|exit|close)\s+(?:of\s+)?([A-Za-z0-9&\-\.: ]+?)\s*$", t, re.I)
    if m:
        sym = NORM(m.group(1))
        tr = S["trades"].pop(sym, None)
        if tr:
            S["closed"].append({"sym": sym, "dir": tr["dir"], "entry": tr.get("entry"), "how": "you closed it", "day": datetime.fromtimestamp(when, IST).strftime("%Y-%m-%d")})
            del S["closed"][:-60]
            return [("👍 Noted - you are out of %s. I stopped tracking it." % sym, None)]
        return [("I was not tracking %s." % sym, None)]
    m = re.match(r"^([A-Za-z0-9&\-\.: ]+?)\s+([0-9]+(?:\.[0-9]+)?)\s*$", t)
    if m:
        sym = NORM(m.group(1))
        tr = S["trades"].get(sym)
        if tr:
            tr["entry"] = float(m.group(2))
            tr["announced"] = 0
            txt, rows = entry_message(sym, tr, " (the price you typed)")
            return [(txt, rows)]
    if low in ("list", "/list", "status", "/status", "trades"):
        if not S["trades"]:
            return [("System 2: no open trades. Signals come with BUY / SELL buttons.", None)]
        lines = []
        for sym, tr in sorted(S["trades"].items()):
            if tr.get("entry") is None:
                lines.append("%s %s (working out the price)" % (word(tr["dir"]), sym))
            else:
                lines.append("%s %s from %.2f | stop %.2f (%.2f pts) | reached 1:%d so far" % (
                    word(tr["dir"]), sym, tr["entry"], stop_price(tr), tr["sl"], tr.get("announced", 0)))
        return [("System 2 open trades:\n" + "\n".join(lines), [[("OUT %s" % s, "out2:" + s)] for s in sorted(S["trades"])])]
    if low in ("help", "/help", "/start", "start", "hi", "hello"):
        return [("System 2 (5 EMA on 30m, 15m chart). Tap BUY / SELL under a signal when you enter. Messages: list | OUT <name> | <name> <real price> | system 1", None)]
    return None


# ----------------------------------- chart picture -----------------------------------
def chart_png(sym, c15, at, sig, path):
    """A simple chart of the last ~2 sessions: 15m candles, the 30m 5 EMA, the signal candle marked. None if matplotlib is missing."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    rows = [c for c in c15 if c[5]][-70:]
    if len(rows) < 10:
        return None
    fig, ax = plt.subplots(figsize=(7.2, 4.2), dpi=110)
    xs = list(range(len(rows)))
    for i, (t, o, h, l, c, done) in enumerate(rows):
        col = "#1a9850" if c >= o else "#d73027"
        ax.plot([i, i], [l, h], color=col, lw=0.8)
        ax.add_patch(plt.Rectangle((i - 0.3, min(o, c)), 0.6, max(abs(c - o), 1e-9), color=col))
    ev = [at(c[0]) for c in rows]
    ex = [x for x, v in zip(xs, ev) if v is not None]
    ey = [v for v in ev if v is not None]
    ax.step(ex, ey, where="post", color="#2166ac", lw=1.6, label="EMA %d (30m)" % EMA_LEN)
    for i, c in enumerate(rows):
        if c[0] == sig["t"]:
            ax.axvline(i, color="#888", lw=0.8, ls="--")
    ticks = [i for i, c in enumerate(rows) if mins(c[0]) == SESSION_START or i == len(rows) - 1]
    ax.set_xticks(ticks)
    ax.set_xticklabels([datetime.fromtimestamp(rows[i][0], IST).strftime("%d %b %H:%M") for i in ticks], fontsize=7)
    ax.set_title("%s %s  15m  close %.2f vs EMA %.2f" % ("BULLISH" if sig["dir"] == 1 else "BEARISH", sym, sig["c"], sig["ema"]), fontsize=10, color="#1a9850" if sig["dir"] == 1 else "#d73027")
    ax.legend(loc="best", fontsize=7)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def send_photo(path, caption):
    try:
        with open(path, "rb") as f:
            requests.post("https://api.telegram.org/bot%s/sendPhoto" % FD.TG_TOKEN, data={"chat_id": FD.TG_CHAT_ID, "caption": caption[:900]},
                          files={"photo": f}, timeout=40)
    except Exception as e:
        print("photo not sent:", e, flush=True)


# ----------------------------------- the run -----------------------------------
def signal_message(sigs, now, free_slots):
    green = sorted([s for s in sigs if s["dir"] == 1], key=lambda s: s["sym"])
    red = sorted([s for s in sigs if s["dir"] == -1], key=lambda s: s["sym"])

    def line(s):
        return "%s  close %.2f | EMA %.2f | candle %s-%s  O %.2f H %.2f L %.2f%s" % (
            s["sym"], s["c"], s["ema"], fmt_time(s["t"]), fmt_time(s["close_ts"]), s["o"], s["h"], s["l"],
            "   ⏰ after 14:45 - no new entry" if s["late"] else "")
    parts = []
    if green:
        parts.append("🟢 BULLISH - closed above the 5 EMA (%d):\n%s" % (len(green), "\n".join(line(s) for s in green)))
    if red:
        parts.append("🔴 BEARISH - closed below the 5 EMA (%d):\n%s" % (len(red), "\n".join(line(s) for s in red)))
    text = "📈 System 2 - 15m candle closed %s (EMA 5 on 30m)\n\n%s" % (fmt_time(max(s["close_ts"] for s in sigs)), "\n\n".join(parts))
    usable = [s for s in green + red if not s["late"]]
    rows = []
    if free_slots <= 0:
        text += "\n\n🚫 Both trade slots are in use - send OUT <name> to free one."
    else:
        for grp, dirn in ((green, 1), (red, -1)):
            keys = [("%s %s %s" % (mark(dirn), word(dirn), s["sym"]), "e2:%d:%s" % (dirn, s["sym"])) for s in grp if not s["late"]]
            rows += [keys[i:i + 2] for i in range(0, len(keys), 2)]
        if usable:
            text += "\n\nTap the button to take the trade; I then ask for the stop loss."
    return text, rows


def run(token, st, now, full=True):
    """full: look at all stocks for new signals (and watch open trades). Otherwise only watch open trades."""
    S = s2(st)
    now_ts = int(now.timestamp())
    today = now.strftime("%Y-%m-%d")
    minute = now.hour * 60 + now.minute
    weekday = now.weekday() < 5
    out = []
    in_session = weekday and SESSION_START <= minute <= SESSION_END + 5
    if not in_session:
        return []
    S["pending"] = {k: v for k, v in S["pending"].items() if datetime.fromtimestamp(v["close_ts"], IST).strftime("%Y-%m-%d") == today}
    watch = sorted(S["trades"])
    syms = sorted(set(FD.short(s) for s in FD.symbols()) | set(watch)) if full else watch
    if not syms:
        return []
    if full and WAIT_FOR_CANDLE:
        WAIT_FOR_CANDLE(token, syms[0], now)
    sigs, charts, errors, stopped = [], {}, [], None
    for n, sym in enumerate(syms):
        if n:
            time.sleep(PACE)
        try:
            raw = FD.history(token, fyers_symbol(sym), resolution="15", days=HIST_DAYS)
        except FD.RateLimited as e:
            stopped = "%s (stopped after %d of %d stocks)" % (e, n, len(syms))
            break
        except FD.FyersError as e:
            errors.append("%s: %s" % (sym, e))
            continue
        c15 = to_candles(raw, now_ts)
        if not c15:
            errors.append("%s: no candles" % sym)
            continue
        at = ema_lookup(c15)
        tr = S["trades"].get(sym)
        if tr is not None:
            out += follow_trade(S, sym, tr, c15, at, now_ts)
        if full and tr is None:
            for sg in find_signals(sym, c15, now_ts, at):
                key = "%s|%d" % (sym, sg["t"])
                if key in S["seen"]:
                    continue
                S["seen"].append(key)
                sigs.append(sg)
                charts[key] = (c15, at)
                S["pending"][sym] = {"dir": sg["dir"], "c": sg["c"], "t": sg["t"], "close_ts": sg["close_ts"], "ema": sg["ema"]}
    del S["seen"][:-400]
    if sigs:
        free = MAX_OPEN - len(S["trades"])
        shown = 0
        for sg in sorted(sigs, key=lambda s: (-s["dir"], s["sym"])):
            if shown >= CHARTS_MAX:
                break
            c15, at = charts["%s|%d" % (sg["sym"], sg["t"])]
            path = chart_png(sg["sym"], c15, at, sg, "s2_chart_%d.png" % shown)
            if path:
                send_photo(path, "%s %s - %s candle %s-%s closed %.2f, EMA %.2f" % (
                    mark(sg["dir"]), sg["sym"], "bullish" if sg["dir"] == 1 else "bearish", fmt_time(sg["t"]), fmt_time(sg["close_ts"]), sg["c"], sg["ema"]))
                shown += 1
        out.append(signal_message(sigs, now, free))
    if S["trades"] and minute >= NOTE_AT_MIN and S.get("note_day") != today:
        S["note_day"] = today
        out.append(("⏰ Market closes at 15:30. System 2 open trades: %s. Close intraday positions yourself before your broker's square-off." % ", ".join(sorted(S["trades"])), None))
    if stopped:
        out.append(("⚠️ " + stopped, None))
    if errors:
        out.append(("⚠️ %d stock problem(s): %s" % (len(errors), "; ".join(errors[:6])), None))
    return out


def follow_trade(S, sym, tr, c15, at, now_ts):
    """Fill a missing entry price, announce new 1:N levels, and close the trade on an exit. Returns [(text, rows)]."""
    out = []
    done = [c for c in c15 if c[5]]
    if tr.get("entry") is None:
        if not done:
            return out
        tr["entry"], tr["ts"] = done[-1][4], done[-1][0] + 900
        tr["announced"] = 0
        txt, rows = entry_message(sym, tr, " (close of the last finished 15m candle %s)" % fmt_time(done[-1][0]))
        return [(txt, rows)]
    levels, ex = evaluate(tr, c15, at)
    last_px = c15[-1][4]
    if levels > tr.get("announced", 0):
        first = tr.get("announced", 0) + 1
        span = "1:%d" % levels if first == levels else "1:%d to 1:%d" % (first, levels)
        out.append(("🎯 %s %s reached %s (price now about %.2f, entry %.2f, %.2f points per R)." % (
            mark(tr["dir"]), sym, span, last_px, tr["entry"], tr["sl"]), [[("OUT %s" % sym, "out2:" + sym)]]))
        tr["announced"] = levels
    if ex:
        r = result_r(tr, ex["price"])
        if ex["kind"] == "stop":
            head, why = "🛑 STOP LOSS touched", "price touched your stop %.2f" % ex["price"]
        elif ex["kind"] == "lock":
            head, why = "💰 BOOK PROFIT at 1:%d" % LOCK_AT_R, "it had reached 1:%d or more and came back to 1:%d (%.2f)" % (LOCK_START_R, LOCK_AT_R, ex["price"])
        else:
            side = "below" if tr["dir"] == 1 else "above"
            head, why = "🚪 EXIT", "a 15m candle (%s) closed %s the EMA: close %.2f vs EMA %.2f" % (fmt_time(ex["t"]), side, ex["price"], ex["ema"])
        out.append(("%s %s %s - %s.\nResult about %+.2fR (%+.2f points per share). Close your position; I stopped tracking it." % (
            head, mark(tr["dir"]), sym, why, r, tr["dir"] * (ex["price"] - tr["entry"])), None))
        S["closed"].append({"sym": sym, "dir": tr["dir"], "entry": tr["entry"], "exit": round(ex["price"], 2), "r": round(r, 2), "how": ex["kind"],
                            "day": datetime.fromtimestamp(now_ts, IST).strftime("%Y-%m-%d")})
        del S["closed"][:-60]
        S["trades"].pop(sym, None)
    return out
