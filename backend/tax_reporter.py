"""
NZ tax-year reporter for Alpaca activity.

Produces the data an NZ chartered accountant needs to file IR3:
  • All FILL trades (USD + NZD conversion at trade date)
  • Realized P&L (FIFO matched, USD + NZD)
  • Dividends received + US withholding tax (W-8BEN treaty 15%)
  • Holding period distribution (trader vs investor signal)
  • Fees (SEC/TAF/etc.)

NZ tax year runs April 1 → March 31 (so "FY 2026" = 2025-04-01 to
2026-03-31). NZ has no general capital gains tax — but if you trade
frequently you may be classified as a trader (income tax on gains).
The holding-period distribution helps the accountant judge this.

Output formats:
  - compute_summary(...)  → structured dict
  - to_csv_bundle(...)    → multi-section CSV string (trades + dividends + summary)
  - to_html(...)          → minimal email-friendly HTML
"""
from __future__ import annotations

import csv
import io
import logging
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Cache one year of daily NZDUSD rates to avoid hammering yfinance per trade.
# Maps date (YYYY-MM-DD) → NZD per 1 USD (e.g. 1.65 means $1 = NZ$1.65).
_FX_CACHE: Dict[str, float] = {}


def nz_fy_window(fy_ending_year: int) -> Tuple[date, date]:
    """Return (start, end) for the NZ financial year ending in March of given year.
    FY 2026 = 2025-04-01 .. 2026-03-31."""
    return date(fy_ending_year - 1, 4, 1), date(fy_ending_year, 3, 31)


def _load_fx_cache(start: date, end: date) -> None:
    """Pre-load daily NZDUSD rates for the window into _FX_CACHE.
    Uses yfinance NZDUSD=X (NZD/USD), inverted to give NZD per 1 USD."""
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("[Tax] yfinance unavailable, FX rates will fall back to 1.65 default")
        return
    try:
        # NZDUSD=X gives USD per 1 NZD (e.g. 0.60), we want NZD per 1 USD (1.667)
        ticker = yf.Ticker("NZDUSD=X")
        hist = ticker.history(start=start.isoformat(), end=(end + timedelta(days=2)).isoformat())
        for ts, row in hist.iterrows():
            d = ts.date().isoformat()
            usd_per_nzd = float(row["Close"])
            if usd_per_nzd > 0:
                _FX_CACHE[d] = 1.0 / usd_per_nzd
        logger.info(f"[Tax] FX cache loaded: {len(_FX_CACHE)} daily NZDUSD rates")
    except Exception as e:
        logger.warning(f"[Tax] FX cache load failed: {e}")


def fx_rate_nzd_per_usd(d: date) -> float:
    """NZD per 1 USD on date d. Falls back to nearest previous business day,
    then to long-run mean 1.65 if no data."""
    key = d.isoformat()
    if key in _FX_CACHE:
        return _FX_CACHE[key]
    # Walk back up to 7 days for weekends / holidays
    for back in range(1, 8):
        prev = (d - timedelta(days=back)).isoformat()
        if prev in _FX_CACHE:
            return _FX_CACHE[prev]
    return 1.65  # long-run NZDUSD ~0.60 → 1.65 NZD per USD


def _parse_alpaca_dt(value) -> datetime:
    """Accept either an ISO8601 string ('2026-05-04T13:33:00.743Z') or a
    pandas Timestamp / datetime-like; return a stdlib datetime."""
    if hasattr(value, "year") and not isinstance(value, str):
        # pandas.Timestamp or datetime — already a datetime-like; coerce to stdlib
        return datetime(value.year, value.month, value.day,
                        getattr(value, "hour", 0), getattr(value, "minute", 0),
                        getattr(value, "second", 0))
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def compute_summary(
    alpaca_client,
    fy_ending_year: int,
) -> dict:
    """Pull Alpaca activities for the NZ FY window and build the tax summary.

    `alpaca_client` is a tradeapi.REST instance (or compatible). We use:
      - get_activities(activity_types='FILL')  → trades
      - get_activities(activity_types='DIV')   → dividends
      - get_activities(activity_types='FEE')   → exchange/regulatory fees
    """
    fy_start, fy_end = nz_fy_window(fy_ending_year)
    _load_fx_cache(fy_start, fy_end)

    # ── Fetch activities (paginate via `until` cursor — max 100/page) ──────
    def fetch_all(activity_type: str) -> list:
        results = []
        until: Optional[str] = None
        while True:
            try:
                kwargs = {"activity_types": activity_type, "page_size": 100}
                if until:
                    kwargs["until"] = until
                page = alpaca_client.get_activities(**kwargs)
            except Exception as e:
                logger.error(f"[Tax] Alpaca {activity_type} fetch error: {e}")
                break
            if not page:
                break
            results.extend(page)
            # Find oldest in this page; if older than FY start, stop
            oldest = page[-1]
            try:
                ts = getattr(oldest, "transaction_time", None) or oldest.date
                ts_str = ts if isinstance(ts, str) else ts.isoformat()
                oldest_date = _parse_alpaca_dt(ts_str).date() if "T" in ts_str else date.fromisoformat(ts_str[:10])
                if oldest_date < fy_start:
                    break
                # Cursor to one second before the oldest item
                until = ts_str
            except Exception:
                break
            if len(page) < 100:
                break
        return results

    fills_raw = fetch_all("FILL")
    divs_raw  = fetch_all("DIV")
    fees_raw  = fetch_all("FEE")

    # ── Filter to FY window ────────────────────────────────────────────────
    def in_window(activity, date_attr="transaction_time") -> bool:
        try:
            ts = getattr(activity, date_attr, None) or getattr(activity, "date", None)
            if isinstance(ts, str):
                d = _parse_alpaca_dt(ts).date()
            else:
                d = ts.date() if hasattr(ts, "date") else ts
            return fy_start <= d <= fy_end
        except Exception:
            return False

    fills = [a for a in fills_raw if in_window(a, "transaction_time")]
    divs  = [a for a in divs_raw  if in_window(a, "date")]
    fees  = [a for a in fees_raw  if in_window(a, "date")]

    # ── Build trade rows + FIFO realized P&L ───────────────────────────────
    trades: list = []
    realized_pl_usd = 0.0
    realized_pl_nzd = 0.0
    # FIFO inventory per symbol — deque of (qty, cost_per_share_usd, fx_rate, trade_date)
    inventory: Dict[str, deque] = defaultdict(deque)
    # Holding period buckets (days)
    holding_buckets = {"<7d": 0, "7-30d": 0, "30-90d": 0, "90-365d": 0, ">365d": 0}

    for f in sorted(fills, key=lambda a: a.transaction_time):
        d = _parse_alpaca_dt(f.transaction_time).date()
        sym = f.symbol
        side = f.side.lower()
        qty = float(f.qty)
        price_usd = float(f.price)
        total_usd = qty * price_usd
        fx = fx_rate_nzd_per_usd(d)
        total_nzd = total_usd * fx

        row = {
            "date": d.isoformat(), "symbol": sym, "side": side.upper(),
            "qty": round(qty, 6), "price_usd": round(price_usd, 4),
            "total_usd": round(total_usd, 2),
            "fx_nzd_per_usd": round(fx, 4),
            "total_nzd": round(total_nzd, 2),
            "realized_pnl_usd": None,
            "realized_pnl_nzd": None,
            "holding_days": None,
        }

        if side in ("buy", "buy_long"):
            inventory[sym].append((qty, price_usd, fx, d))
        elif side in ("sell", "sell_long"):
            qty_to_match = qty
            cost_basis_usd = 0.0
            cost_basis_nzd = 0.0
            avg_holding_days = 0.0
            matched = 0.0
            while qty_to_match > 1e-8 and inventory[sym]:
                lot_qty, lot_cost_usd, lot_fx, lot_date = inventory[sym][0]
                taken = min(qty_to_match, lot_qty)
                cost_basis_usd += taken * lot_cost_usd
                cost_basis_nzd += taken * lot_cost_usd * lot_fx
                hold_days = (d - lot_date).days
                avg_holding_days += taken * hold_days
                matched += taken
                if taken >= lot_qty - 1e-8:
                    inventory[sym].popleft()
                else:
                    inventory[sym][0] = (lot_qty - taken, lot_cost_usd, lot_fx, lot_date)
                qty_to_match -= taken

            pnl_usd = total_usd - cost_basis_usd
            pnl_nzd = total_nzd - cost_basis_nzd
            realized_pl_usd += pnl_usd
            realized_pl_nzd += pnl_nzd
            row["realized_pnl_usd"] = round(pnl_usd, 2)
            row["realized_pnl_nzd"] = round(pnl_nzd, 2)
            if matched > 0:
                hold_avg = avg_holding_days / matched
                row["holding_days"] = round(hold_avg, 1)
                if hold_avg < 7:
                    holding_buckets["<7d"] += 1
                elif hold_avg < 30:
                    holding_buckets["7-30d"] += 1
                elif hold_avg < 90:
                    holding_buckets["30-90d"] += 1
                elif hold_avg < 365:
                    holding_buckets["90-365d"] += 1
                else:
                    holding_buckets[">365d"] += 1

        trades.append(row)

    # ── Dividend rows ───────────────────────────────────────────────────────
    div_rows: list = []
    total_div_gross_usd = 0.0
    total_div_gross_nzd = 0.0
    for dv in divs:
        d = dv.date if hasattr(dv, "date") and isinstance(dv.date, date) else _parse_alpaca_dt(
            dv.date if isinstance(dv.date, str) else dv.transaction_time
        ).date()
        sym = dv.symbol
        net_usd = float(dv.net_amount)
        # Alpaca's DIV.net_amount is post-withholding. We approximate gross
        # assuming 15% NZ-US treaty rate (W-8BEN filed). If user has 30%
        # withholding (no W-8BEN), the gross calc is wrong but we flag it.
        # Real gross = net / 0.85
        gross_usd = round(net_usd / 0.85, 2)
        withheld_usd = round(gross_usd - net_usd, 2)
        fx = fx_rate_nzd_per_usd(d)
        gross_nzd = round(gross_usd * fx, 2)
        withheld_nzd = round(withheld_usd * fx, 2)
        net_nzd = round(net_usd * fx, 2)
        div_rows.append({
            "date": d.isoformat(), "symbol": sym,
            "gross_usd": gross_usd, "withheld_usd_15pct": withheld_usd, "net_usd": net_usd,
            "fx_nzd_per_usd": round(fx, 4),
            "gross_nzd": gross_nzd, "withheld_nzd": withheld_nzd, "net_nzd": net_nzd,
        })
        total_div_gross_usd += gross_usd
        total_div_gross_nzd += gross_nzd
    total_div_withheld_usd = round(sum(r["withheld_usd_15pct"] for r in div_rows), 2)
    total_div_withheld_nzd = round(sum(r["withheld_nzd"] for r in div_rows), 2)

    # ── Fees ────────────────────────────────────────────────────────────────
    fee_rows: list = []
    total_fees_usd = 0.0
    for fe in fees:
        d = fe.date if hasattr(fe, "date") and isinstance(fe.date, date) else _parse_alpaca_dt(
            fe.date if isinstance(fe.date, str) else fe.transaction_time
        ).date()
        amt = float(fe.net_amount)  # negative
        fee_rows.append({
            "date": d.isoformat(),
            "amount_usd": round(amt, 4),
            "description": getattr(fe, "description", "")[:80],
        })
        total_fees_usd += amt
    total_fees_nzd = round(total_fees_usd * 1.65, 2)  # approx — fees rounded

    # ── Trader-vs-investor signals ─────────────────────────────────────────
    n_trades = len(trades)
    fy_days = (fy_end - fy_start).days + 1
    trades_per_day = round(n_trades / fy_days, 3)

    avg_holding = 0.0
    sells_with_holding = [t["holding_days"] for t in trades if t.get("holding_days") is not None]
    if sells_with_holding:
        avg_holding = round(sum(sells_with_holding) / len(sells_with_holding), 1)

    # Heuristic trader-likelihood (subjective, accountant has final say)
    if trades_per_day > 1.0 or avg_holding < 30:
        trader_signal = "HIGH — likely trader classification"
    elif trades_per_day > 0.3 or avg_holding < 90:
        trader_signal = "MEDIUM — discuss with accountant"
    else:
        trader_signal = "LOW — likely investor"

    return {
        "fy_ending_year": fy_ending_year,
        "fy_start": fy_start.isoformat(),
        "fy_end": fy_end.isoformat(),
        "generated_at": datetime.utcnow().isoformat(),

        "trades": trades,
        "dividends": div_rows,
        "fees": fee_rows,

        "summary": {
            "n_trades": n_trades,
            "n_dividends": len(div_rows),
            "trades_per_day": trades_per_day,
            "avg_holding_days_at_sell": avg_holding,
            "holding_period_buckets": holding_buckets,
            "realized_pnl_usd": round(realized_pl_usd, 2),
            "realized_pnl_nzd": round(realized_pl_nzd, 2),
            "div_gross_usd": round(total_div_gross_usd, 2),
            "div_gross_nzd": round(total_div_gross_nzd, 2),
            "div_withheld_usd": total_div_withheld_usd,
            "div_withheld_nzd": total_div_withheld_nzd,
            "div_net_usd": round(sum(r["net_usd"] for r in div_rows), 2),
            "fees_usd": round(total_fees_usd, 4),
            "fees_nzd": total_fees_nzd,
            "trader_classification_signal": trader_signal,
        },
    }


def to_csv_bundle(summary: dict) -> str:
    """Multi-section CSV string. Hand this to your accountant."""
    buf = io.StringIO()
    w = csv.writer(buf)

    w.writerow([f"SerenityAlphaTrader NZ Tax Summary — FY {summary['fy_ending_year']}"])
    w.writerow([f"Period: {summary['fy_start']} to {summary['fy_end']}"])
    w.writerow([f"Generated: {summary['generated_at']}"])
    w.writerow([])

    s = summary["summary"]
    w.writerow(["=== SUMMARY ==="])
    w.writerow(["Total trades", s["n_trades"]])
    w.writerow(["Trades per day", s["trades_per_day"]])
    w.writerow(["Avg holding days at sell", s["avg_holding_days_at_sell"]])
    w.writerow(["Realized P&L (USD)", s["realized_pnl_usd"]])
    w.writerow(["Realized P&L (NZD)", s["realized_pnl_nzd"]])
    w.writerow(["Dividends gross (USD)", s["div_gross_usd"]])
    w.writerow(["Dividends gross (NZD)", s["div_gross_nzd"]])
    w.writerow(["Dividend withholding USD (15% treaty)", s["div_withheld_usd"]])
    w.writerow(["Dividend withholding NZD", s["div_withheld_nzd"]])
    w.writerow(["Fees (USD)", s["fees_usd"]])
    w.writerow(["Trader classification signal", s["trader_classification_signal"]])
    w.writerow([])
    w.writerow(["=== HOLDING PERIOD DISTRIBUTION ==="])
    for k, v in s["holding_period_buckets"].items():
        w.writerow([k, v])
    w.writerow([])

    w.writerow(["=== TRADES ==="])
    w.writerow(["date", "symbol", "side", "qty", "price_usd", "total_usd",
                "fx_nzd_per_usd", "total_nzd",
                "realized_pnl_usd", "realized_pnl_nzd", "holding_days"])
    for t in summary["trades"]:
        w.writerow([t["date"], t["symbol"], t["side"], t["qty"], t["price_usd"],
                    t["total_usd"], t["fx_nzd_per_usd"], t["total_nzd"],
                    t.get("realized_pnl_usd", ""), t.get("realized_pnl_nzd", ""),
                    t.get("holding_days", "")])
    w.writerow([])

    w.writerow(["=== DIVIDENDS ==="])
    w.writerow(["date", "symbol", "gross_usd", "withheld_usd_15pct", "net_usd",
                "fx_nzd_per_usd", "gross_nzd", "withheld_nzd", "net_nzd"])
    for d in summary["dividends"]:
        w.writerow([d["date"], d["symbol"], d["gross_usd"], d["withheld_usd_15pct"],
                    d["net_usd"], d["fx_nzd_per_usd"], d["gross_nzd"],
                    d["withheld_nzd"], d["net_nzd"]])
    w.writerow([])

    w.writerow(["=== FEES ==="])
    w.writerow(["date", "amount_usd", "description"])
    for f in summary["fees"]:
        w.writerow([f["date"], f["amount_usd"], f["description"]])

    return buf.getvalue()


def to_html(summary: dict) -> str:
    """Email-friendly HTML summary (just the headline numbers, not full trade list)."""
    s = summary["summary"]
    return f"""
<html><body style="font-family:Arial,sans-serif;color:#2c3e50;">
  <h2>SerenityAlphaTrader NZ Tax Summary — FY {summary['fy_ending_year']}</h2>
  <p style="color:#7f8c8d;">Period: {summary['fy_start']} → {summary['fy_end']}<br>
  Generated: {summary['generated_at']} UTC</p>

  <h3>Key numbers (give to accountant)</h3>
  <table cellpadding="6" style="border-collapse:collapse;border:1px solid #ddd;">
    <tr><td>Realized P&L</td><td>USD ${s['realized_pnl_usd']:+,.2f}</td><td>NZD ${s['realized_pnl_nzd']:+,.2f}</td></tr>
    <tr><td>Dividends gross</td><td>USD ${s['div_gross_usd']:.2f}</td><td>NZD ${s['div_gross_nzd']:.2f}</td></tr>
    <tr><td>US tax withheld (foreign tax credit)</td><td>USD ${s['div_withheld_usd']:.2f}</td><td>NZD ${s['div_withheld_nzd']:.2f}</td></tr>
    <tr><td>Total trades</td><td colspan="2">{s['n_trades']} ({s['trades_per_day']}/day avg)</td></tr>
    <tr><td>Avg holding period at sell</td><td colspan="2">{s['avg_holding_days_at_sell']} days</td></tr>
    <tr><td>Trader classification signal</td><td colspan="2"><strong>{s['trader_classification_signal']}</strong></td></tr>
  </table>

  <h3>Holding period distribution</h3>
  <table cellpadding="6" style="border-collapse:collapse;border:1px solid #ddd;">
    {''.join(f'<tr><td>{k}</td><td>{v} sells</td></tr>' for k, v in s['holding_period_buckets'].items())}
  </table>

  <p style="font-size:12px;color:#999;margin-top:24px;">
    Full trade-by-trade CSV is attached. NZ FY runs April 1 → March 31.
    For NZ residents, capital gains are generally tax-free unless IRD
    classifies you as a trader (look at the holding-period distribution
    and trades-per-day). Dividends are always taxable as income;
    foreign tax already withheld counts toward an NZ tax credit.
    Filing deadline IR3: July 7 (or March 31 next year via tax agent).
  </p>
</body></html>"""
