#!/usr/bin/env python3
"""news_watch.py — proactive breaking-news monitor for our holdings + themes.

Gap found 2026-06-23 (user: "did we catch the Korea chip crash?"): the system
pulls news on-demand but never proactively ALERTS on material market events.
This Exa-searches our current holdings + key thesis sectors (memory/HBM, CPO/
optics, Korea, Fed/rates), flags items carrying material-risk keywords, and
emails the user a short alert. Decision-support only — it never trades; the
engine's -5% stop + regime exposure handle actual risk. Cron a few times/day.
"""
import sys, os, re, smtplib, subprocess, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database import SessionLocal, get_setting, set_setting
from email.mime.text import MIMEText
import requests

A = "https://api.alpaca.markets"
MCPORTER = "/data/qbao775/miniconda3/bin/mcporter"
# Negative (risk) and positive (catalyst) keywords kept SEPARATE so alerts are
# labeled correctly (a deal/upgrade is good news, not a risk). Matched on WORD
# BOUNDARIES via regex so short tokens don't false-match inside words (e.g. "cut"
# must not match "Connecticut", "deal" not "dealer", "beat" not "unbeatable").
RISK_KW = ("plunge", "crash", "selloff", "sell-off", "tumble", "slump", "rout",
           "downgrade", "cut", "warning", "warn", "miss", "glut", "oversupply",
           "circuit breaker", "halt", "slash", "weak", "disappoints", "probe", "lawsuit",
           "layoff", "layoffs", "job cuts", "restructuring", "export control", "tariff",
           "antitrust", "sanction", "recall")
POS_KW = ("partnership", "deal", "agreement", "contract", "surges", "soars",
          "record high", "upgrade", "investment", "wins", "beats", "raises guidance",
          "acquire", "acquisition", "merger", "buyout", "release", "launch", "unveil",
          "collaborat", "executive order", "subsidy", "tax credit", "funding round")
_RISK_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in RISK_KW) + r")\b", re.I)
_POS_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in POS_KW) + r")\b", re.I)


def log(m): print(f"{datetime.datetime.utcnow().isoformat()}Z  {m}", flush=True)


# 2026-07-01: user wants autonomous analysis, not raw headlines requiring him
# to ask Claude every time ("你需要自己不断的分析，不要我一直提醒你"). A cloud
# /schedule agent CANNOT reach local Alpaca creds / Exa / this DB (sandboxed,
# repo-only), so the automated read has to happen HERE, locally, using the
# already-running gemma4:31b (promoted by the earlier LLM shootout, listening
# on :11435 — no extra GPU spin-up needed). This is a lightweight heuristic
# pass, NOT a full multi-agent Serenity workflow (too slow/costly to run 4x/
# day unattended) — it only flags candidates for the 5%-cap satellite; it
# NEVER trades and Claude still does the deep read + gets user confirmation
# before anything is bought.
OLLAMA_HOST = "http://localhost:11435"
OLLAMA_MODEL = "gemma4:31b"

def quick_chokepoint_take(fresh_items):
    """One combined Ollama call: for each fresh headline, a one-line verdict
    on whether it's a real supply-chain/catalyst event worth flagging for the
    5% Serenity satellite, vs noise. Returns plain text or "" on failure."""
    if not fresh_items:
        return ""
    listing = "\n".join(f"- {a}" for a in fresh_items)
    prompt = (
        "You are a terse semiconductor/AI supply-chain analyst using Serenity's "
        "chokepoint lens (real bottleneck = concentrated suppliers, long "
        "qualification cycles, hard capex — not just a headline with a big name "
        "in it). For EACH headline below, respond with exactly one line:\n"
        "  <ticker or 'N/A'> | <REAL BOTTLENECK / NOISE / TOO EARLY> | <one clause why>\n\n"
        f"{listing}\n\n"
        "Only list tickers that trade on Alpaca (US-listed) or are clearly "
        "identifiable. Be skeptical by default — most headlines are noise."
    )
    try:
        r = requests.post(f"{OLLAMA_HOST}/api/generate",
                          json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
                          timeout=120)
        if r.status_code == 200:
            return r.json().get("response", "").strip()
    except Exception as e:
        log(f"quick_chokepoint_take failed: {e}")
    return ""


def exa(query, n=4):
    try:
        # mcporter's shebang is `#!/usr/bin/env node`, and both mcporter and
        # node live in the base miniconda /bin, not the `alphatrader` conda
        # env's /bin. Discovered 2026-07-01: this silently returned rc=127
        # "node: No such file or directory" whenever cron/backend ran inside
        # the alphatrader env, making news_watch fire 0 alerts for months.
        env = dict(os.environ)
        env["PATH"] = "/data/qbao775/miniconda3/bin:" + env.get("PATH", "")
        r = subprocess.run([MCPORTER, "call", "exa.web_search_exa",
                            f"query={query}", f"numResults={n}"],
                           capture_output=True, text=True, timeout=90,
                           cwd="/data/qbao775/AlphaTrader", env=env)
        if r.returncode != 0:
            log(f"exa rc={r.returncode} stderr={r.stderr[:200]!r}")
        return r.stdout
    except Exception as e:
        log(f"exa fail: {e}"); return ""


# 2026-07-12: user asked why a Meta model launch (Llama -> rebranded "Muse
# Spark") wasn't caught, then "你不是有关注meta的官网吗" (don't you watch
# Meta's official blog?) -- honest answer was no: everything above is
# generic Exa web search gated on a hardcoded keyword list (RISK_KW/POS_KW),
# which breaks every time a product/model gets renamed (whack-a-mole, not a
# durable fix). This adds DIRECT monitoring of the major AI labs' own blogs,
# independent of guessing keywords: real RSS feeds where available (OpenAI,
# DeepMind), Exa site-restricted search as a fallback where a lab has no
# working RSS (checked live 2026-07-12: ai.meta.com and anthropic.com don't
# expose one). Any post found this way is inherently alert-worthy -- it's
# the lab's own announcement -- so it bypasses the RISK_KW/POS_KW title
# filter entirely rather than depending on the post's title happening to
# contain one of those words (e.g. "Introducing Muse Spark 1.1" matches
# neither list).
OFFICIAL_BLOG_RSS = {
    "OpenAI":    "https://openai.com/blog/rss.xml",
    "DeepMind":  "https://deepmind.google/blog/rss.xml",
    # added 2026-07-12 (user: SKHY/MU/SanDisk 官网也要重点关注) -- both have
    # real, working RSS feeds (verified live), same treatment as OpenAI/DeepMind.
    "Micron":    "https://investors.micron.com/rss/news-releases.xml",
    "SK hynix":  "https://news.skhynix.com/feed/",
}
OFFICIAL_BLOG_SITE_SEARCH = {
    "Meta AI":   "site:ai.meta.com/blog",
    "Anthropic": "site:anthropic.com/news",
    # SanDisk's newsroom has no working RSS (checked live 2026-07-12 --
    # rss.xml redirects to a dead preview host) -- Exa site-search fallback.
    "SanDisk":   "site:sandisk.com/company/newsroom",
}


def _fetch_rss_titles(url, n=5):
    """Minimal RSS parse (title + link per <item>) -- avoids adding a
    feedparser dependency for what's a simple, well-formed feed."""
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        items = re.findall(r"<item>(.*?)</item>", r.text, re.S)
        out = []
        for it in items[:n]:
            tm = re.search(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", it, re.S)
            lm = re.search(r"<link>\s*(.*?)\s*</link>", it, re.S)
            if tm:
                out.append((tm.group(1).strip(), lm.group(1).strip() if lm else ""))
        return out
    except Exception as e:
        log(f"rss fetch failed ({url}): {e}")
        return []


def check_official_blogs():
    """Returns a list of '🆕 [来源] Title' strings for posts detected on
    monitored AI-lab blogs, independent of the generic keyword filter."""
    found = []
    for name, url in OFFICIAL_BLOG_RSS.items():
        for title, _link in _fetch_rss_titles(url):
            found.append(f"🆕 [{name} 官方博客] {title[:150]}")
    for name, site_q in OFFICIAL_BLOG_SITE_SEARCH.items():
        raw = exa(f"{site_q} latest blog post announcement", 5)
        for line in raw.splitlines():
            m = re.match(r"\s*Title:\s*(.+)", line)
            if m:
                found.append(f"🆕 [{name} 官方博客] {m.group(1).strip()[:150]}")
    return found


def email(db, subject, body):
    try:
        s = get_setting(db, "email_sender", 1, ""); pw = get_setting(db, "email_app_password", 1, "")
        r = get_setting(db, "email_recipient", 1, "")
        if not (s and pw and r): return
        msg = MIMEText(body, "plain", "utf-8"); msg["Subject"] = subject
        msg["From"] = s; msg["To"] = r
        srv = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20)
        srv.login(s, pw); srv.send_message(msg); srv.quit()
        log(f"emailed: {subject}")
    except Exception as e:
        log(f"email failed: {e}")


def main():
    db = SessionLocal()
    H = {"APCA-API-KEY-ID": get_setting(db, "alpaca_api_key", 1, ""),
         "APCA-API-SECRET-KEY": get_setting(db, "alpaca_secret_key", 1, "")}
    pos = requests.get(f"{A}/v2/positions", headers=H, timeout=15).json()
    held = sorted({p["symbol"] for p in pos if float(p.get("qty", 0)) > 0.001})
    held_q = " ".join(held)

    # 2026-07-01: user explicitly said catalyst tracking (new model releases,
    # layoffs, government policy, M&A, NVIDIA partnerships) is HIGHEST PRIORITY
    # for spotting fast satellite opportunities. This feeds the 5% Serenity
    # satellite trigger (see PLAN_D.md "Optional Serenity satellite") — it is
    # an ALERT feed, not an auto-trader. Claude reviews each fresh hit, does a
    # quick chokepoint read, and proposes (never auto-executes) a satellite
    # trade for the user to confirm.
    queries = [
        (f"{held_q} stock news today", "我们的持仓"),
        ("semiconductor memory HBM DRAM SK Hynix Samsung Micron selloff news today", "内存/半导体板块"),
        ("AI optics CPO co-packaged optics Coherent Lumentum AAOI news today", "CPO/光通信板块"),
        ("quantum computing stocks IONQ RGTI QBTS QUBT momentum Trump executive order news today", "量子(观察,不持仓)"),
        ("new AI model release GPT Gemini Claude Muse Spark Grok Meta Superintelligence Labs announcement this week", "新模型发布"),
        ("tech company layoffs 2026 Google Meta Amazon Microsoft Intel job cuts", "大公司裁员"),
        ("CHIPS Act semiconductor export control tariff policy AI executive order 2026", "政府政策"),
        ("tech company acquisition merger buyout AI startup 2026", "并购/收购"),
        ("NVIDIA partnership deal collaboration announcement 2026", "NVIDIA 合作伙伴"),
    ]
    # keep headlines carrying a material keyword (risk OR positive), labeled 🔴/🟢
    alerts = []
    for q, label in queries:
        raw = exa(q, 4)
        for line in raw.splitlines():
            m = re.match(r"\s*Title:\s*(.+)", line)
            if not m:
                continue
            title = m.group(1).strip()
            is_risk = bool(_RISK_RE.search(title)); is_pos = bool(_POS_RE.search(title))
            if is_risk or is_pos:
                tag = "🔴" if is_risk else "🟢"   # risk wins the tag if both present
                alerts.append(f"{tag} [{label}] {title[:150]}")

    # Official-blog posts bypass the RISK_KW/POS_KW filter entirely -- these
    # are curated, always-relevant sources (the lab's own announcement), not
    # generic web search results that need keyword gating.
    alerts.extend(check_official_blogs())

    # de-dup, and only alert on items not seen before (stored signature)
    alerts = list(dict.fromkeys(alerts))
    seen = set(filter(None, get_setting(db, "news_watch_seen", 1, "").split("||")))
    fresh = [a for a in alerts if a not in seen]
    if not fresh:
        log(f"no fresh material news ({len(alerts)} headlines, all seen)")
        return

    set_setting(db, "news_watch_seen", "||".join((list(seen) + fresh)[-60:]), 1)

    verdict = quick_chokepoint_take(fresh)
    verdict_block = (f"\n\n🤖 自动卡点初判 (gemma4:31b, 仅供参考, 非最终结论):\n{verdict}\n"
                      if verdict else "")

    body = ("检测到与你持仓/板块相关的重大动态(🔴=风险 / 🟢=利好;仅提醒,引擎不会"
            "自动交易):\n\n  • " + "\n  • ".join(fresh) +
            verdict_block +
            f"\n\n当前持仓: {held_q}\n"
            "如果初判显示 REAL BOTTLENECK,回复我具体股票,我会用完整 Serenity "
            "框架深度分析,再决定是否用 5% 卫星仓小额尝试(上限约 $3,000,你确认才下单)。")
    email(db, f"📰 持仓相关动态 {len(fresh)} 条 — SerenityAlphaTrader", body)
    log(f"ALERTED {len(fresh)} fresh items")


if __name__ == "__main__":
    main()
