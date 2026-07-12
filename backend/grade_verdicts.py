#!/usr/bin/env python3
"""
grade_verdicts.py — checks whether the local BULLISH/BEARISH calls logged by
crossvalidate_satellite.py's log_verdict() actually turned out to be right.

User (2026-07-12): "你觉得有没有可以改进你的算法的方式" (do you see any way to
improve the algorithm). Up to now the system produced 4-master and Serenity
verdicts constantly but nothing ever checked afterward whether either lens
was actually predictive — so there was no feedback loop telling us whether
the local screening is trustworthy, degrading, or which of the two lenses
(4-master vs Serenity) is more reliable. This closes that loop.

Logic:
  - Read every line in .verdict_log.jsonl (one record per gradable verdict:
    symbol, source, direction, price_at_verdict, timestamp).
  - A record becomes gradable once GRADE_HORIZON_DAYS has passed since it was
    logged (enough time for the direction to actually show up in price).
  - Grade it: fetch the current price, compute % change since the verdict,
    call it CORRECT if the direction matches the move beyond a noise
    threshold, WRONG if it moved the opposite way beyond that threshold,
    INCONCLUSIVE if it stayed within the noise band either way.
  - Never re-grade the same record (tracked by a stable id in a small state
    file) and never mutate the append-only log.
  - Aggregate a rolling accuracy % per source (4master_hold, serenity_hold,
    4master_candidate, serenity_candidate) so we can see which lens is
    actually earning its keep, not just which one talks the most.

Read-only with respect to positions/orders — this only grades PAST calls and
reports; it never trades and never changes CANDIDATE_WATCHLIST or holdings.
"""
import sys, os, json, datetime, hashlib
sys.path.insert(0, '/data/qbao775/AlphaTrader/backend')

STACK_DIR = os.path.realpath('/home/qbao775/serenity-trader-stack')
VERDICT_LOG = f'{STACK_DIR}/.verdict_log.jsonl'
GRADED_STATE = f'{STACK_DIR}/.verdict_graded_state.json'
SCORECARD_MD = f'{STACK_DIR}/PREDICTION_ACCURACY.md'

GRADE_HORIZON_DAYS = 14     # give the call this long to actually play out
NOISE_BAND_PCT = 3.0        # moves smaller than this in either direction don't count as a hit or a miss


def log(msg):
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    print(f"[{ts}] {msg}", flush=True)


def rec_id(rec):
    key = f"{rec['ts']}|{rec['symbol']}|{rec['source']}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def load_graded():
    if os.path.exists(GRADED_STATE):
        try:
            return json.load(open(GRADED_STATE))
        except Exception:
            return {}
    return {}


def save_graded(graded):
    with open(GRADED_STATE, 'w') as f:
        json.dump(graded, f, indent=2)


def get_current_price(symbol):
    try:
        import market_data as md
        q = md.get_stock_quote(symbol) or {}
        return q.get('current')
    except Exception as e:
        log(f"  price lookup failed for {symbol}: {e}")
        return None


def send_email(subject, body):
    import smtplib
    from email.mime.text import MIMEText
    from database import SessionLocal, get_setting
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
    if not os.path.exists(VERDICT_LOG):
        log("no verdict log yet — nothing to grade")
        return

    graded = load_graded()
    now = datetime.datetime.utcnow()
    new_grades = []

    with open(VERDICT_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            rid = rec_id(rec)
            if rid in graded:
                continue
            ts = datetime.datetime.fromisoformat(rec['ts'])
            age_days = (now - ts).total_seconds() / 86400
            if age_days < GRADE_HORIZON_DAYS:
                continue

            current = get_current_price(rec['symbol'])
            if not current:
                continue  # try again next run rather than grade on missing data

            change_pct = (current / rec['price_at_verdict'] - 1) * 100
            if rec['direction'] == 'BULLISH':
                outcome = 'CORRECT' if change_pct >= NOISE_BAND_PCT else \
                          'WRONG' if change_pct <= -NOISE_BAND_PCT else 'INCONCLUSIVE'
            else:  # BEARISH
                outcome = 'CORRECT' if change_pct <= -NOISE_BAND_PCT else \
                          'WRONG' if change_pct >= NOISE_BAND_PCT else 'INCONCLUSIVE'

            graded[rid] = {**rec, 'graded_at': now.isoformat(), 'price_now': current,
                            'change_pct': change_pct, 'outcome': outcome}
            new_grades.append(graded[rid])

    if not new_grades:
        log("no records reached the grading horizon this run")
        return

    save_graded(graded)

    # aggregate rolling accuracy per source, across ALL graded history (not just this run)
    by_source = {}
    for rec in graded.values():
        if rec['outcome'] == 'INCONCLUSIVE':
            continue
        s = by_source.setdefault(rec['source'], {'correct': 0, 'total': 0})
        s['total'] += 1
        if rec['outcome'] == 'CORRECT':
            s['correct'] += 1

    ts = now.strftime('%Y-%m-%d')
    lines = [f"\n## {ts}", f"- 本次新评分 {len(new_grades)} 条 (评分窗口: 发出判断后{GRADE_HORIZON_DAYS}天)"]
    for rec in new_grades:
        mark = {'CORRECT': '✅', 'WRONG': '❌', 'INCONCLUSIVE': '➖'}[rec['outcome']]
        lines.append(f"  {mark} {rec['symbol']} [{rec['source']}] {rec['direction']} @ "
                      f"${rec['price_at_verdict']:.2f} -> ${rec['price_now']:.2f} "
                      f"({rec['change_pct']:+.1f}%) = {rec['outcome']}")
    lines.append("- 累计各来源准确率(排除INCONCLUSIVE):")
    for src, s in sorted(by_source.items()):
        acc = s['correct'] / s['total'] * 100 if s['total'] else 0
        lines.append(f"  - {src}: {s['correct']}/{s['total']} = {acc:.0f}%")

    with open(SCORECARD_MD, 'a') as f:
        f.write("\n".join(lines) + "\n")

    body = ("本轮新评分的历史判断,以及累计准确率(仅供参考,不影响任何自动交易):\n\n"
            + "\n".join(l.lstrip() for l in lines[1:]) +
            "\n\n说明: CORRECT=方向判断对且波动超过噪音阈值; WRONG=方向判断反了;"
            " INCONCLUSIVE=波动太小不算数,不计入准确率。这个只是给你和我自己看哪个"
            "框架(4大师 vs Serenity)更靠谱,不会自动调整仓位或止损。")
    send_email(f"📊 判断准确率复盘 ({ts})", body)
    summary = ", ".join(f"{k}={v['correct']}/{v['total']}" for k, v in by_source.items())
    log(f"graded {len(new_grades)} new record(s); by-source accuracy: {summary}")


if __name__ == '__main__':
    main()
