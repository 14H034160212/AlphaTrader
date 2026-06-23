"""Serenity supply-chain chokepoint lens — PRIMARY decision framework.

Distilled from the `serenity-aleabitoreddit` skill (5,857 tweets of trader
Serenity / @aleabitoreddit, an AI/semiconductor supply-chain analyst). Per the
user's 2026-06-08 directive, this lens LEADS stock selection: the brain reasons
through Serenity's chokepoint methodology FIRST, and the platform's prior
methods (technicals, DCF, RL feedback, the old large-cap-first prior) become
SUPPORTING evidence used to pressure-test the Serenity thesis — never to
override it.

Decision-support only. This module produces prompt text; it never trades,
places, or cancels orders. Serenity's self-reported returns are unverified and
carry survivorship/selection bias; his names are volatile micro/small-caps.

Data sources (read once, cached):
  .claude/skills/serenity-aleabitoreddit/data/ticker_stats.txt   — his universe
  .claude/skills/serenity-aleabitoreddit/references/methodology.md — the checklist
  .claude/skills/serenity-aleabitoreddit/references/track-record.md — dated calls
"""
import os
import re
import json
import logging

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SKILL_DIR = os.path.join(_REPO_ROOT, ".claude", "skills", "serenity-aleabitoreddit")
_UNIVERSE_PATH = os.path.join(_SKILL_DIR, "data", "ticker_stats.txt")
_TWEETS_PATH = os.path.join(_SKILL_DIR, "data", "aleabitoreddit_tweets.json")
# LIVE tweets pulled directly from @aleabitoreddit via Agent Reach
# (fetch_serenity_tweets.sh, cron every 6h) — unfreezes the 2026-06-08 archive.
# Merged into recency scoring so his FRESHEST tweet becomes the decay anchor.
_LIVE_TWEETS_PATH = os.path.join(_SKILL_DIR, "data", "serenity_latest_tweets.json")
# Fresh CURRENT-focus tickers scraped daily from semiconstocks.com tracker
# (refresh_serenity_intel.py) — supplements the frozen 2026-06-08 tweet archive.
_FOCUS_PATH = os.path.join(_SKILL_DIR, "data", "serenity_current_focus.json")
# Smart-money + influencer signals (Buffett/Pelosi/Trump/Musk 13F + tweets) from
# fetch_smart_money.sh — used ONLY as a conviction cross-check on names Serenity
# already covers (never introduces off-thesis mega-caps). See _smart_money_confirms.
_SMART_MONEY_PATH = os.path.join(_SKILL_DIR, "data", "smart_money_signals.json")
# Recency half-life (days) for time-decayed conviction scoring: a mention this
# many days before his latest tweet counts half as much. 30d → strongly favors
# what he's pushing NOW (user directive 2026-06-10: latest tweets = top priority,
# priority decays with age).
_RECENCY_HALFLIFE_DAYS = 30.0
_METHODOLOGY_PATH = os.path.join(_SKILL_DIR, "references", "methodology.md")
_TRACK_RECORD_PATH = os.path.join(_SKILL_DIR, "references", "track-record.md")

# Conviction tiers by total mention count in his feed (a rough proxy — he tweets
# most about his highest-conviction bottleneck names).
_TIER_CORE = 150       # flagship / repeatedly-pressed bottleneck thesis
_TIER_COVERED = 20     # a real, recurring name in his coverage
_TIER_MENTIONED = 2    # named at least a couple of times


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.warning("serenity_lens: could not read %s (%s)", path, e)
        return ""


def _load_universe():
    """Parse ticker_stats.txt → {TICKER: {'mentions': int, 'first': str, 'last': str}}."""
    out = {}
    text = _read(_UNIVERSE_PATH)
    for line in text.splitlines():
        parts = line.split()
        # rows look like: "AXTI        541   2025-12-22  2026-06-07"
        if len(parts) == 4 and parts[1].isdigit() and "-" in parts[2]:
            out[parts[0].upper()] = {
                "mentions": int(parts[1]),
                "first": parts[2],
                "last": parts[3],
            }
    return out


def _extract_checklist():
    """Pull the '## 15. The checklist' section verbatim from methodology.md."""
    text = _read(_METHODOLOGY_PATH)
    if not text:
        return ""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith("## 15."):
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


# Hot-reloadable cache: re-read the data files whenever their mtime changes, so a
# background refresh (refresh_serenity_data.sh pulling yan-labs latest) is picked
# up by the RUNNING engine with no restart. Falls back to last-good on read error.
_cache = {"universe": None, "uni_mtime": None, "checklist": None, "chk_mtime": None}


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _get_universe():
    m = _mtime(_UNIVERSE_PATH)
    if _cache["universe"] is None or m != _cache["uni_mtime"]:
        loaded = _load_universe()
        if loaded or _cache["universe"] is None:
            _cache["universe"] = loaded
            _cache["uni_mtime"] = m
    return _cache["universe"]


def _get_checklist():
    m = _mtime(_METHODOLOGY_PATH)
    if _cache["checklist"] is None or m != _cache["chk_mtime"]:
        loaded = _extract_checklist()
        if loaded or _cache["checklist"] is None:
            _cache["checklist"] = loaded
            _cache["chk_mtime"] = m
    return _cache["checklist"]


def _ticker_track_record(symbol, limit=6):
    """Grep track-record.md for dated-call rows where the ticker column matches."""
    sym = (symbol or "").upper()
    if not sym:
        return []
    rows = []
    for line in _read(_TRACK_RECORD_PATH).splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # table is: Date | Ticker | Call | Outcome
        if len(cells) >= 4 and cells[1].upper() == sym:
            rows.append((cells[0], cells[2], cells[3]))
    # most recent last in the file (chronological) → take the tail
    return rows[-limit:]


def get_ticker_stance(symbol):
    """Return a structured read of Serenity's stance on `symbol`.

    Keys: in_universe (bool), tier (str), mentions (int), first/last (str),
    calls (list of (date, call, outcome)).
    """
    sym = (symbol or "").upper()
    # Strip exchange suffix for HK/CN names (e.g. 2382.HK) — his universe is US.
    base = sym.split(".")[0]
    universe = _get_universe()
    info = universe.get(sym) or universe.get(base)
    if not info:
        return {"in_universe": False, "tier": "NOT-COVERED", "mentions": 0,
                "first": None, "last": None, "calls": []}
    m = info["mentions"]
    tier = ("CORE" if m >= _TIER_CORE else
            "COVERED" if m >= _TIER_COVERED else
            "MENTIONED" if m >= _TIER_MENTIONED else "PASSING")
    return {
        "in_universe": True, "tier": tier, "mentions": m,
        "first": info["first"], "last": info["last"],
        "calls": _ticker_track_record(sym) or _ticker_track_record(base),
    }


def build_serenity_lens_block(symbol, sector="Other"):
    """Build the PRIMARY decision-framework section injected into analyze_stock.

    The block (a) states the chokepoint mental model as the lead lens, (b) gives
    Serenity's actual stance on this ticker if he covers it, and (c) hands the
    model his checklist to vet any name. Supporting platform signals
    (technicals/DCF/RL) appear later in the prompt as pressure-test inputs.
    """
    stance = get_ticker_stance(symbol)

    header = (
        "## 🧭 SERENITY SUPPLY-CHAIN CHOKEPOINT LENS — PRIMARY FRAMEWORK\n"
        "*(User directive 2026-06-08: reason through THIS lens FIRST. The "
        "technicals, DCF, RL feedback and prior 'large-cap-first' notes below are "
        "SUPPORTING evidence to pressure-test the chokepoint thesis — they do NOT "
        "override it. Decision-support only: never copy his trades; his returns "
        "are self-reported/unverified with survivorship bias.)*\n\n"
        "**The core mental model:** Do NOT default to the obvious 'shovel seller' "
        "(e.g. NVDA). Trace the supply chain UPSTREAM to the single chokepoint a "
        "hyperscaler will pay anything to keep flowing — optical/CPO, InP "
        "substrate/epiwafer, memory/HBM, AI power/grid, packaging/equipment. The "
        "further upstream and the smaller the market cap, the more mispriced the "
        "bottleneck tends to be relative to the trillions of AI capex flowing "
        "downstream. **Overlooked small/mid-cap upstream chokepoints in the "
        "user's focus themes (semis / GPU-downstream supply chain / physical AI / "
        "robotics) are explicitly IN-SCOPE and PREFERRED here — this overrides any "
        "generic 'avoid small-cap' prior for these themes.**\n"
    )

    # Serenity's actual stance on this specific name.
    if stance["in_universe"]:
        tier_label = {
            "CORE": "🔥 CORE conviction name (flagship bottleneck thesis he presses repeatedly)",
            "COVERED": "✅ COVERED — a recurring name in his coverage",
            "MENTIONED": "• MENTIONED a handful of times",
            "PASSING": "· passing reference only (low weight)",
        }.get(stance["tier"], stance["tier"])
        lines = [
            f"\n**Serenity's stance on {symbol}:** {tier_label} — "
            f"{stance['mentions']} mentions, {stance['first']} → {stance['last']}."
        ]
        if stance["calls"]:
            lines.append("His dated calls (self-reported outcomes — treat as calibration, not proof):")
            for date, call, outcome in stance["calls"]:
                lines.append(f"  - [{date}] {call} → {outcome}")
        else:
            lines.append("(No dated-call rows recorded for this ticker; weight on mention frequency only.)")
        stance_block = "\n".join(lines) + "\n"
    else:
        stance_block = (
            f"\n**Serenity's stance on {symbol}:** NOT in his covered universe. "
            "Do NOT fabricate a view for him. Instead, vet this name FRESH against "
            "his checklist below — the more boxes it ticks, the more it fits a true "
            "chokepoint; few boxes = likely not his kind of setup.\n"
        )

    # Smart-money cross-check (Buffett/Berkshire 13F, Congress/Pelosi/Trump, Musk
    # tweets). Confirmation only — appears only for names Serenity ALSO covers.
    sm_block = ""
    if symbol in _smart_money_confirms():
        sm_block = (
            f"\n**🐳 Smart-money cross-check:** {symbol} is ALSO currently held/pushed "
            "by tracked smart money (Buffett/Berkshire 13F, Congress, or Musk/Trump) — "
            "a dual-confirmation with Serenity's chokepoint thesis. Mild conviction "
            "boost only; lagged disclosure, NOT independent proof, never a reason to "
            "chase a name that's already run.\n"
        )

    checklist_block = ""
    _checklist = _get_checklist()
    if _checklist:
        checklist_block = (
            "\n### Serenity's checklist (apply to this name; more 'yes' = stronger chokepoint fit)\n"
            + _checklist + "\n"
        )

    decision_guidance = (
        "\n**How to let the lens lead the decision:**\n"
        "- If the name is a genuine upstream chokepoint in a focus theme with "
        "checklist confirmation and a dated catalyst → that is the strongest BUY "
        "case, even at small/mid cap. Size to conviction × research depth, not to hype.\n"
        "- If it's the obvious downstream 'shovel seller' (mega-cap) already "
        "crowded/frontrun → the lens is skeptical of NEW capital; HOLD if owned, "
        "don't chase. (Still honor the LONG-TERM HOLD mandate for quality names already owned.)\n"
        "- If it fails the chokepoint test and isn't in a focus theme → default HOLD.\n"
        "- **LAGGARD PREFERENCE (user 2026-06-10):** within the NVDA/CPO supply chain, "
        "PREFER names trading well BELOW their 52-week high (the un-run laggard the surge "
        "rotates to next) over names already near 52w highs (already surged — don't chase). "
        "Use the 52w-range and MA200 distance in the data above: far from high + intact "
        "chokepoint thesis = higher-priority BUY; near 52w high + already +100% = HOLD/skip.\n"
        "- ALWAYS confirm current price/fundamentals yourself; his theses decay and "
        "his small-caps are volatile (20%+ days, dilution/single-customer/binary risk).\n"
    )

    return header + stance_block + sm_block + checklist_block + decision_guidance


_TICK_RE = re.compile(r"\$([A-Za-z]{1,6})\b")
_recency_cache = {"scores": None, "mtime": None}


def _compute_recency_scores():
    """Time-decayed mention score per ticker from the tweet archive.

    weight(tweet) = 0.5 ** (age_days / HALFLIFE), where age is measured from the
    LATEST tweet in the archive (so scoring is stable as long as data is stable,
    and 'recent' means recent relative to the freshest data we have). Summed per
    $ticker across tweet text + quoted text. → his CURRENT focus floats to top.
    """
    import datetime as _dt
    tweets = []
    # archive (frozen 2026-06-08) + LIVE tweets (cron-refreshed). The live file's
    # fresh timestamps become the recency anchor, so his latest picks dominate.
    for path in (_TWEETS_PATH, _LIVE_TWEETS_PATH):
        txt = _read(path)
        if not txt:
            continue
        try:
            t = json.loads(txt)
            if isinstance(t, list):
                tweets.extend(t)
        except Exception as e:
            logger.warning("serenity_lens: tweet parse failed (%s): %s", path, e)
    if not tweets:
        return {}

    def _ts(t):
        iso = t.get("createdAtISO") or ""
        try:
            return _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except Exception:
            return None

    stamps = [s for s in (_ts(t) for t in tweets if isinstance(t, dict)) if s]
    if not stamps:
        return {}
    ref = max(stamps)  # anchor recency to the freshest tweet
    scores = {}
    for t in tweets:
        if not isinstance(t, dict):
            continue
        ts = _ts(t)
        if not ts:
            continue
        age_days = (ref - ts).total_seconds() / 86400.0
        w = 0.5 ** (age_days / _RECENCY_HALFLIFE_DAYS)
        txt = (t.get("text", "") or "") + " " + ((t.get("quotedTweet") or {}).get("text", "") or "")
        for m in set(_TICK_RE.findall(txt)):
            scores[m.upper()] = scores.get(m.upper(), 0.0) + w
    return scores


def _get_recency_scores():
    m = (_mtime(_TWEETS_PATH), _mtime(_LIVE_TWEETS_PATH))
    if _recency_cache["scores"] is None or m != _recency_cache["mtime"]:
        computed = _compute_recency_scores()
        if computed or _recency_cache["scores"] is None:
            _recency_cache["scores"] = computed
            _recency_cache["mtime"] = m
    return _recency_cache["scores"]


_focus_cache = {"focus": None, "mtime": None}


def _get_current_focus():
    """Fresh current-focus tickers from the daily semiconstocks scrape (or [])."""
    m = _mtime(_FOCUS_PATH)
    if _focus_cache["focus"] is None or m != _focus_cache["mtime"]:
        try:
            with open(_FOCUS_PATH) as f:
                _focus_cache["focus"] = json.load(f).get("top_focus", []) or []
        except Exception:
            _focus_cache["focus"] = []
        _focus_cache["mtime"] = m
    return _focus_cache["focus"]


def _smart_money_confirms():
    """Tickers that smart money (Buffett/Berkshire 13F, Congress/Pelosi/Trump
    trades, Musk/Trump tweets) ALSO favors AND that are already in Serenity's
    universe — i.e. dual-confirmation. INTERSECTION ONLY: a smart-money name not
    in Serenity's coverage (e.g. GOOGL/AMZN mega-caps) is never introduced here,
    so this can only boost on-thesis names, never pull us off-thesis. Returns a
    set; empty on any failure (so the lens degrades to pure-Serenity)."""
    try:
        with open(_SMART_MONEY_PATH) as f:
            sm = json.load(f)
        # overlap_with_serenity = smart-money tickers ∩ Serenity's CURRENT focus —
        # the right set (e.g. NVDA). Intersect once more with current focus for
        # robustness. Deliberately NOT the all-time universe (which contains
        # mega-caps like GOOGL Serenity merely mentioned once, off current thesis).
        focus = set(_get_current_focus())
        confirms = set(sm.get("overlap_with_serenity", []))
        confirms |= (set(sm.get("tickers", [])) & focus)
        return confirms
    except Exception:
        return set()


def recommended_tickers(top_n=45, min_score=0.5):
    """Serenity's CURRENT conviction names, ranked by RECENCY-decayed mentions.

    User directive 2026-06-10: prioritise what he is pushing in his LATEST tweets;
    priority decays with tweet age (30-day half-life). Returns up to `top_n`
    tickers whose recency score >= `min_score`, highest-conviction-now first.
    Drives the watchlist so the engine focuses on his current thesis (e.g. the
    CPO/optics chain — SIVE/AAOI/LITE/SOI/JBL — rather than stale all-time names).
    The per-stock lens still decides BUY/HOLD and surfaces 'do-not-chase' calls.
    Falls back to all-time mention count if the tweet archive is unavailable.
    """
    # Fresh current-focus (daily semiconstocks scrape) goes FIRST — it reflects
    # what Serenity is pushing NOW, fresher than the frozen 2026-06-08 archive.
    focus = _get_current_focus()
    scores = _get_recency_scores()
    # Smart-money cross-check: a 1.25x conviction boost for names Serenity AND
    # smart money both favor (e.g. NVDA). It nudges ranking — Serenity's own
    # strongest picks still lead — and only touches names already in his universe.
    confirms = _smart_money_confirms()
    if not scores:  # fallback: all-time mentions
        archive = [t for t, info in sorted(_get_universe().items(),
                                           key=lambda kv: -kv[1]["mentions"])
                   if info["mentions"] >= 50]
    else:
        boosted = {t: (s * 1.25 if t in confirms else s) for t, s in scores.items()}
        archive = [t for t, s in sorted(boosted.items(), key=lambda kv: -kv[1]) if s >= min_score]
    # merge: fresh focus first, then archive-recency, deduped
    return list(dict.fromkeys(focus + archive))[:top_n]


# NVDA-downstream quality names NOT in Serenity's optics-focused coverage, but
# that fit the user's "NVDA-downstream laggard" thesis (2026-06-10). SECONDARY
# priority — appended AFTER Serenity's own picks so his analysis stays #1.
NVDA_DOWNSTREAM_EXTRAS = [
    "VRT",   # Vertiv — power/cooling, NVDA co-designed, $15B backlog, relatively un-run
    "FN",    # Fabrinet — NVDA optical-module assembly (picks-and-shovels)
    "SMCI",  # Supermicro — NVDA rack/server systems
]


def nvda_downstream_extras():
    """Secondary candidate pool: NVDA-downstream laggards outside Serenity's
    coverage. Lower priority than recommended_tickers() — Serenity stays #1."""
    return list(NVDA_DOWNSTREAM_EXTRAS)


def universe_size():
    """Number of distinct tickers in Serenity's loaded universe (for diagnostics)."""
    return len(_get_universe())
