"""Read-only login diagnosis. Sends NOTHING that changes the bot, consumes no code, spends no FYERS login.
Prints a report in the GitHub run log AND sends the same report to your Telegram."""
import os, json, base64, time, hashlib
from datetime import datetime, timezone, timedelta
import requests

IST = timezone(timedelta(hours=5, minutes=30))
TG = os.environ.get("TG_TOKEN", ""); CHAT = os.environ.get("TG_CHAT_ID", "")
GH = os.environ.get("GITHUB_TOKEN", ""); REPO = os.environ.get("GITHUB_REPOSITORY", "")
out = []

def say(s=""):
    print(s, flush=True); out.append(s)

def clock(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%d %b %H:%M:%S")

def claims(code):
    try:
        p = code.split(".")[1]; p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}

now = time.time()
say("DIAGNOSIS at %s IST" % clock(now))

# 1. Telegram: webhook + waiting messages (getUpdates with offset 0 does NOT delete anything)
say("\n[1] TELEGRAM")
try:
    w = requests.get("https://api.telegram.org/bot%s/getWebhookInfo" % TG, timeout=20).json().get("result", {})
    say("webhook url: %s | pending: %s | last error: %s" % (w.get("url") or "none", w.get("pending_update_count"), w.get("last_error_message") or "none"))
except Exception as e:
    say("webhook check failed: %s" % e)
try:
    r = requests.get("https://api.telegram.org/bot%s/getUpdates" % TG, params={"offset": 0, "timeout": 0}, timeout=20).json()
    ups = r.get("result", []) if r.get("ok") else []
    say("messages Telegram still holds for the bot: %d" % len(ups))
    for u in ups[-15:]:
        m = u.get("message") or {}
        t = (m.get("text") or "")
        kind = "CODE-LIKE" if "eyJ" in t or len(t) > 200 else "text"
        c = claims(t.strip()) if kind == "CODE-LIKE" else {}
        extra = ""
        if c.get("iat"):
            extra = " | FYERS made it %s, valid until %s, age now %d min" % (clock(c["iat"]), clock(c["exp"]) if c.get("exp") else "?", (now - c["iat"]) // 60)
        say("  update %s | sent %s | %s | %d chars%s" % (u.get("update_id"), clock(m.get("date", 0)), kind, len(t), extra))
    if not ups:
        say("  (none: the bot has already read everything)")
except Exception as e:
    say("getUpdates failed: %s" % e)

# 2. Bot memory file
say("\n[2] BOT MEMORY (stock_state.json)")
try:
    st = json.load(open("stock_state.json"))
    say("telegram bookmark tg_offset: %s" % st.get("tg_offset"))
    say("last login prompt sent: %s" % (clock(st["login_prompt_ts"]) if st.get("login_prompt_ts") else "0 (none/reset)"))
    say("refresh tried on day: %s" % st.get("refresh_day"))
except Exception as e:
    say("cannot read stock_state.json: %s" % e)

# 3. Recent GitHub runs of the stocks workflow
say("\n[3] RECENT RUNS (last 40, newest first)")
try:
    h = {"Authorization": "Bearer " + GH, "Accept": "application/vnd.github+json"}
    runs = requests.get("https://api.github.com/repos/%s/actions/runs" % REPO, headers=h, params={"per_page": 40}, timeout=30).json().get("workflow_runs", [])
    for x in runs:
        st_ = datetime.fromisoformat(x["run_started_at"].replace("Z", "+00:00")).timestamp()
        en = datetime.fromisoformat(x["updated_at"].replace("Z", "+00:00")).timestamp()
        say("  #%s %s | start %s | took %d min | %s/%s | %s" % (x["run_number"], x["name"][:14], clock(st_), (en - st_) // 60, x["event"], x["conclusion"] or x["status"], x.get("display_title", "")[:25]))
except Exception as e:
    say("cannot list runs: %s" % e)

# 4. Token file
say("\n[4] TOKEN CACHE")
p = ".token/fyers_token.json"
try:
    t = json.load(open(p)); say("token saved for day %s at %s" % (t.get("day"), clock(t.get("ts", 0))))
except Exception:
    say("no token file in this run (normal if not logged in today)")
say("FYERS app id set: %s | secret set: %s | PIN set: %s" % (bool(os.environ.get("FYERS_APP_ID")), bool(os.environ.get("FYERS_APP_SECRET")), bool(os.environ.get("FYERS_PIN"))))

try:
    requests.post("https://api.telegram.org/bot%s/sendMessage" % TG, data={"chat_id": CHAT, "text": "\n".join(out)[:4000]}, timeout=20)
except Exception:
    pass
