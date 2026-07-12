#!/usr/bin/env python3
"""
ai_model_adoption_watch.py — weekly read on WHICH AI models/labs people are
actually using, not just which ones announced something.

User (2026-07-12): "我觉得可以多关注那些大家都在用的ai模型是哪家的" (pay more
attention to which AI models everyone is actually using). This is a
different signal from check_official_blogs() in news_watch.py -- that
tracks announcements ("what did the lab just ship"); this tracks adoption
("is anyone actually using it"). Adoption is the more forward-looking
signal for the chokepoint thesis: a model genuinely winning usage share
pulls compute/memory demand toward whoever's infra it runs on, ahead of
that showing up in an earnings call.

Sources searched (Exa, same mcporter path as fetch_smart_money.sh/news_watch.py):
  - consumer app rankings (ChatGPT/Gemini/Claude/Meta AI app store standing)
  - developer/API mindshare (OpenRouter usage leaderboards, Hugging Face
    trending models)
  - web traffic comparisons (chatgpt.com vs gemini.google.com vs claude.ai)

This does NOT feed CANDIDATE_WATCHLIST directly (unlike weekly_focus_review.py) --
most labs mentioned here (OpenAI, Anthropic) aren't independently investable,
and the public-market names that ARE tied to them (META/GOOGL/MSFT/AMZN) are
core-allocation names, not satellite candidates, so folding them into the
satellite screen would blur that boundary. This is purely an awareness feed:
a weekly email + a rolling log so the trend (who's gaining/losing usage
share) is visible over time.
"""
import sys, os, re, json, datetime, subprocess
sys.path.insert(0, '/data/qbao775/AlphaTrader/backend')

from database import SessionLocal, get_setting

MCPORTER = "/data/qbao775/miniconda3/bin/mcporter"
OUT_JSON = '/data/qbao775/AlphaTrader/.claude/skills/serenity-aleabitoreddit/data/ai_model_adoption_signals.json'
LOG_MD = os.path.realpath('/home/qbao775/serenity-trader-stack') + '/AI_ADOPTION_LOG.md'

QUERIES = [
    ("ChatGPT vs Gemini vs Claude vs Meta AI app store ranking downloads this week 2026", "消费端App排名"),
    ("OpenRouter LLM model usage leaderboard market share 2026", "开发者/API调用份额"),
    ("chatgpt.com vs gemini.google.com vs claude.ai web traffic comparison 2026", "网站流量对比"),
    ("enterprise AI copilot adoption Microsoft Copilot Google Gemini usage report 2026", "企业级采用率"),
]

# lab/product name -> the public-market name(s) whose infra/revenue benefits
# from that lab winning usage share (not a 1:1 ticker for the lab itself --
# OpenAI/Anthropic aren't independently investable).
LAB2BENEFICIARY = {
    "chatgpt": "OpenAI (via MSFT infra/investment)", "openai": "OpenAI (via MSFT infra/investment)",
    "gemini": "Google/GOOGL", "google ai": "Google/GOOGL", "deepmind": "Google/GOOGL",
    "claude": "Anthropic (via GOOGL/AMZN investment)", "anthropic": "Anthropic (via GOOGL/AMZN investment)",
    "meta ai": "Meta/META", "muse spark": "Meta/META", "llama": "Meta/META",
    "copilot": "Microsoft/MSFT",
}


def log(msg):
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    print(f"[{ts}] {msg}", flush=True)


def exa(query, n=5):
    try:
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
        log(f"exa fail: {e}")
        return ""


def send_email(subject, body):
    import smtplib
    from email.mime.text import MIMEText
    db = SessionLocal()
    sender = get_setting(db, "email_sender", 1, "")
    pw = get_setting(db, "email_app_password", 1, "")
    recip = get_setting(db, "email_recipient", 1, "")
    db.close()
    if not (sender and pw and recip):
        log("email skipped: email_sender/email_app_password/email_recipient not set in DB")
        return
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recip
        s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20)
        s.login(sender, pw)
        s.sendmail(sender, [recip], msg.as_string())
        s.quit()
        log(f"email sent to {recip}")
    except Exception as e:
        log(f"email err: {e}")


def main():
    log("AI model adoption weekly scan starting")
    mention_counts = {}
    highlights = []

    for q, label in QUERIES:
        raw = exa(q, 5)
        titles_this_query = []
        for line in raw.splitlines():
            m = re.match(r"\s*Title:\s*(.+)", line)
            if m:
                title = m.group(1).strip()
                titles_this_query.append(title)
                low = title.lower()
                for name, beneficiary in LAB2BENEFICIARY.items():
                    if name in low:
                        mention_counts[beneficiary] = mention_counts.get(beneficiary, 0) + 1
        if titles_this_query:
            highlights.append((label, titles_this_query[:4]))

    ranked = sorted(mention_counts.items(), key=lambda kv: -kv[1])

    out = {
        "fetched_at": datetime.datetime.utcnow().isoformat() + "Z",
        "beneficiary_mentions": dict(ranked),
        "note": "Adoption/usage signal, distinct from official-blog announcement monitoring. "
                "Awareness-only -- does not feed CANDIDATE_WATCHLIST directly.",
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, 'w') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    ts = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    lines = [f"\n## {ts}"]
    if ranked:
        lines.append("- 本周提及热度排序 (受益标的): " +
                      ", ".join(f"{b}({c})" for b, c in ranked))
    else:
        lines.append("- 本周没有抓到明确的采用率信号 (搜索结果不含已知模型/产品名)")
    for label, titles in highlights:
        lines.append(f"- {label}:")
        for t in titles:
            lines.append(f"  - {t[:140]}")

    with open(LOG_MD, 'a') as f:
        f.write("\n".join(lines) + "\n")

    body = ("每周AI模型采用率/使用热度扫描(区别于官方博客监控——这个看的是\"谁真的在被用\"，"
            "不是\"谁刚发布了什么\"):\n\n" + "\n".join(l.lstrip("- ") for l in lines[1:]) +
            "\n\n说明: 这只是关注度信号,不会自动加入卫星观察名单"
            "(大部分实验室本身不能直接投资,能投资的对应标的如META/GOOGL/MSFT都是核心仓位,"
            "不是卫星候选,避免混淆两套名单的边界)。")
    send_email(f"🤖 AI模型采用率周报 ({ts})", body)
    log(f"done: {len(ranked)} beneficiary mention(s) tracked")

    try:
        stack_dir = os.path.dirname(LOG_MD)
        subprocess.run(['git', 'add', LOG_MD], cwd=stack_dir, check=True)
        if subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=stack_dir).returncode != 0:
            subprocess.run(['git', 'commit', '-m', f"chore: AI model adoption weekly scan {ts}"],
                            cwd=stack_dir, check=True)
            subprocess.run(['git', 'push'], cwd=stack_dir, check=True)
            log("committed + pushed log")
    except subprocess.CalledProcessError as e:
        log(f"git commit/push failed: {e}")


if __name__ == '__main__':
    main()
