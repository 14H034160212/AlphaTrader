#!/usr/bin/env python3
"""Pull Serenity's CURRENT focus from the semiconstocks.com tracker (a public,
no-auth, server-rendered page that the maintainers update as he posts).

The yan-labs tweet archive froze at 2026-06-08 (its scraper stalled). This gives
the engine a fresher *supplement*: the tracker's ticker emphasis ≈ what Serenity
is pushing now. Writes serenity_current_focus.json next to the skill data so
serenity_lens can surface/boost his latest names. Graceful: keeps last-good on
any fetch/parse failure (never clobbers with empty).

Run: python3 refresh_serenity_intel.py   (cron daily)
"""
import os, re, json, datetime
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", ".claude", "skills", "serenity-aleabitoreddit",
                   "data", "serenity_current_focus.json")
URL = "https://semiconstocks.com/"
UA = {"User-Agent": "Mozilla/5.0 (SerenityAlphaTrader intel refresh)"}

# Common words that match $[A-Z]{1,6} but aren't tickers — drop them.
_NOISE = {"AI", "CPO", "EML", "CW", "HBM", "NAND", "GPU", "TAM", "ARR", "US",
          "USD", "Q1", "Q2", "Q3", "Q4", "CEO", "TLDR", "RFQ", "M&A", "YTD"}


def _load_last_good():
    try:
        with open(OUT) as f:
            return json.load(f)
    except Exception:
        return None


def main():
    try:
        html = requests.get(URL, headers=UA, timeout=25).text
    except Exception as e:
        print(f"FETCH_FAIL {e}; keeping last-good")
        return
    if len(html) < 5000:
        print(f"FETCH_THIN {len(html)} bytes; keeping last-good")
        return

    # Ticker emphasis = frequency of $TICK mentions on the tracker page.
    counts = {}
    for m in re.findall(r"\$([A-Za-z]{1,6})\b", html):
        t = m.upper()
        if t in _NOISE or not t.isalpha():
            continue
        counts[t] = counts.get(t, 0) + 1
    ranked = [t for t, c in sorted(counts.items(), key=lambda kv: -kv[1]) if c >= 3]

    # Best-effort: the most recent date string mentioned on the page.
    # Bug fix (2026-07-12): this used to take max() over the raw strings,
    # mixing "2026-06-09" (ISO) and "June 9, 2026" (text) formats -- string
    # comparison sorts by first character, so any text-format date starting
    # with a letter that sorts high (e.g. "May...") beat EVERY ISO date
    # regardless of which was actually more recent. Confirmed live 2026-07-12:
    # real ISO dates on the page went up to 2026-07-05, but the old max()
    # reported "May 13, 2026" purely because 'M' > '2' and 'M' > other
    # date-text letters in ASCII. Parse each candidate into an actual date
    # and compare those; skip unparseable strings or dates further in the
    # future than tomorrow (footer/banner noise, not real content dates).
    today = datetime.date.today()
    candidates = []
    for m in re.findall(r"(20\d{2}-\d{2}-\d{2})", html):
        try:
            candidates.append(datetime.datetime.strptime(m, "%Y-%m-%d").date())
        except ValueError:
            continue
    for m in re.findall(r"([A-Z][a-z]+ \d{1,2},? 20\d{2})", html):
        try:
            candidates.append(datetime.datetime.strptime(m.replace(",", ""), "%B %d %Y").date())
        except ValueError:
            continue
    candidates = [d for d in candidates if d <= today + datetime.timedelta(days=1)]
    src_date = max(candidates).isoformat() if candidates else "unknown"

    if len(ranked) < 5:
        print(f"PARSE_THIN only {len(ranked)} tickers; keeping last-good")
        return

    out = {
        "fetched_at": None,  # stamped by caller wrapper if needed; cron uses file mtime
        "source": URL,
        "source_date_hint": src_date,
        "top_focus": ranked[:25],
        "mention_counts": {t: counts[t] for t in ranked[:25]},
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    print(f"OK top_focus({len(ranked)}): {', '.join(ranked[:12])} | src≈{src_date}")


if __name__ == "__main__":
    main()
