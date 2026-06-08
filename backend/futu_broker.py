"""
Futu OpenD broker integration.

Supports:
  • China A-shares (Shanghai SH / Shenzhen SZ)
  • Hong Kong stocks (HKEX)
  • US stocks via Futu US account

Prerequisites (set in Settings):
  • futu_host  (default: 127.0.0.1)
  • futu_port  (default: 11111)
  • futu_trade_env  "REAL" or "SIMULATE" (default SIMULATE for safety)
  • futu_cn_acc_id   – CN trade account ID (optional; picks first account if blank)
  • futu_hk_acc_id   – HK trade account ID
  • futu_us_acc_id   – US trade account ID

Install futu-api SDK:
  pip install futu-api
Then launch Futu OpenD from https://openapi.futunn.com/
"""
from __future__ import annotations

import logging
from typing import Optional, Dict, List

from broker_interface import BrokerInterface
from market_calendar import detect_market, is_china_ashare, is_hk_stock

logger = logging.getLogger(__name__)

# ── Symbol conversion helpers ─────────────────────────────────────────────────

def _to_futu_code(symbol: str) -> str:
    """
    Convert SerenityTrader symbol to Futu code format.
      600519.SH  →  SH.600519
      000001.SZ  →  SZ.000001
      0700.HK    →  HK.00700   (pad to 5 digits)
      9988.HK    →  HK.09988
      AAPL       →  US.AAPL
    """
    if "." not in symbol:
        return f"US.{symbol.upper()}"

    code, suffix = symbol.rsplit(".", 1)
    suffix = suffix.upper()

    if suffix in ("SH", "SS"):
        return f"SH.{code}"
    if suffix == "SZ":
        return f"SZ.{code}"
    if suffix == "HK":
        # HK codes are 5-digit zero-padded
        padded = code.zfill(5)
        return f"HK.{padded}"
    # Fallback: use as-is
    return f"{suffix}.{code}"


def _futu_market(symbol: str) -> str:
    """Return Futu market string: 'CN', 'HK', or 'US'."""
    mkt = detect_market(symbol)
    if mkt == "CN":
        return "CN"
    if mkt == "HK":
        return "HK"
    return "US"


# ── Futu Broker ───────────────────────────────────────────────────────────────

class FutuBroker(BrokerInterface):
    """
    Futu OpenD broker.  Gracefully degrades to paper mode if futu-api is not
    installed or OpenD daemon is not running.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        trade_env: str = "SIMULATE",   # "REAL" or "SIMULATE"
        cn_acc_id: int = 0,
        hk_acc_id: int = 0,
        us_acc_id: int = 0,
        security_firm: str = "FUTUSECURITIES",
        trade_password: str = "",      # Moomoo trading password (required for REAL HK/CN unlock_trade)
    ):
        self.host = host
        self.port = port
        self._trade_env = trade_env
        self._cn_acc_id = cn_acc_id
        self._hk_acc_id = hk_acc_id
        self._us_acc_id = us_acc_id
        self._security_firm_name = security_firm
        self._trade_password = trade_password

        self._futu_available = False
        self._TrdEnv = None
        self._TrdSide = None
        self._OrderType = None
        self._TrdMarket = None
        self._SecurityFirm = None
        self._ft = None

        try:
            import futu as ft
            self._ft = ft
            self._TrdEnv = ft.TrdEnv.REAL if trade_env == "REAL" else ft.TrdEnv.SIMULATE
            self._TrdSide = ft.TrdSide
            self._OrderType = ft.OrderType
            self._TrdMarket = ft.TrdMarket
            # Resolve security_firm string → enum. Loudly warn on typos —
            # silent fallback to FUTUSECURITIES hides Moomoo NZ/AU/SG accounts.
            if not hasattr(ft.SecurityFirm, security_firm):
                logger.warning(
                    f"[Futu] Unknown security_firm '{security_firm}'. Valid: "
                    f"FUTUSECURITIES (HK), FUTUAU (Moomoo AU/NZ), FUTUSG, "
                    f"FUTUINC (US), FUTUJP, FUTUMY, FUTUCA. "
                    f"Falling back to FUTUSECURITIES — your REAL account may be invisible."
                )
            self._SecurityFirm = getattr(ft.SecurityFirm, security_firm,
                                          ft.SecurityFirm.FUTUSECURITIES)

            # Refuse REAL with missing per-market acc_id: passing acc_id=0 to
            # Moomoo means "default account" which can silently route to the
            # wrong account in multi-account setups. SIMULATE is fine — it has
            # exactly one account per env.
            if trade_env == "REAL":
                missing = [m for m, aid in (("HK", hk_acc_id), ("US", us_acc_id), ("CN", cn_acc_id))
                           if aid in (0, None)]
                if len(missing) == 3:
                    raise RuntimeError(
                        "[Futu] REAL mode requires at least one of "
                        "futu_{hk,us,cn}_acc_id to be set. All are 0/missing. "
                        "Refusing to start — would silently route to wrong account."
                    )
                if missing:
                    logger.warning(
                        f"[Futu] REAL mode: no acc_id for markets {missing}. "
                        f"Orders for those markets will use Moomoo's default account."
                    )

            self._futu_available = True
            logger.info(
                f"[Futu] futu-api loaded. OpenD target: {host}:{port} "
                f"env={trade_env} security_firm={security_firm}"
            )
        except ImportError:
            logger.warning("[Futu] futu-api not installed. Run: pip install futu-api. Using paper fallback.")

    # ── BrokerInterface ──────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "Futu"

    @property
    def supported_markets(self) -> List[str]:
        # 2026-05-21: extended from [CN,HK,US] after probing actual Moomoo AU
        # OpenD which returned live accounts for HK/US/AU. JP/CN/SG require
        # extra Moomoo-side KYC activation but the SDK enums + code path
        # already work — once the user enables them, no further code change.
        return ["CN", "HK", "US", "AU", "JP", "SG"]

    def is_connected(self) -> bool:
        if not self._futu_available:
            return False
        try:
            ft = self._ft
            with ft.OpenQuoteContext(host=self.host, port=self.port) as ctx:
                ret, data = ctx.get_global_state()
                return ret == ft.RET_OK
        except Exception as e:
            logger.debug(f"[Futu] Connection check failed: {e}")
            return False

    def _trade_ctx(self, market: str):
        """Build OpenSecTradeContext for the given market (CN/HK/US).

        Replaces deprecated OpenHKTradeContext / OpenUSTradeContext /
        OpenCNTradeContext which were removed from futu-api 10.x. Same wire
        protocol, just a unified entry point with explicit market filter and
        security_firm (NZ accounts need FUTUAU, HK needs FUTUSECURITIES, etc.).
        """
        ft = self._ft
        market_enum = {
            "CN": ft.TrdMarket.CN,
            "HK": ft.TrdMarket.HK,
            "US": ft.TrdMarket.US,
            "AU": ft.TrdMarket.AU,
            "JP": ft.TrdMarket.JP,
            "SG": ft.TrdMarket.SG,
        }.get(market, ft.TrdMarket.US)
        return ft.OpenSecTradeContext(
            filter_trdmarket=market_enum,
            host=self.host, port=self.port,
            security_firm=self._SecurityFirm,
        )

    def get_account(self) -> Dict:
        if not self._futu_available:
            return {"cash": 0.0, "equity": 0.0, "buying_power": 0.0, "currency": "USD"}
        try:
            ft = self._ft
            # Try CN account first, then HK, then US
            for market, acc_id, currency in [
                ("CN", self._cn_acc_id, "CNY"),
                ("HK", self._hk_acc_id, "HKD"),
                ("US", self._us_acc_id, "USD"),
            ]:
                try:
                    with self._trade_ctx(market) as ctx:
                        ret, data = ctx.accinfo_query(
                            trd_env=self._TrdEnv,
                            acc_id=acc_id or 0,
                        )
                        if ret == ft.RET_OK and not data.empty:
                            row = data.iloc[0]
                            return {
                                "cash": float(row.get("cash", 0)),
                                "equity": float(row.get("total_assets", 0)),
                                "buying_power": float(row.get("avl_withdrawal_cash", 0)),
                                "currency": currency,
                                "market": market,
                            }
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[Futu] get_account error: {e}")
        return {"cash": 0.0, "equity": 0.0, "buying_power": 0.0, "currency": "USD"}

    def submit_buy(self, symbol: str, quantity: float, price: float) -> Dict:
        return self._place_order(symbol, quantity, price, side="BUY")

    def submit_sell(self, symbol: str, quantity: float, price: float) -> Dict:
        return self._place_order(symbol, quantity, price, side="SELL")

    def _place_order(self, symbol: str, quantity: float, price: float, side: str) -> Dict:
        if not self._futu_available:
            return {"success": False, "order_id": None, "error": "futu-api not installed"}

        ft = self._ft
        futu_code = _to_futu_code(symbol)
        market = _futu_market(symbol)
        trd_side = self._TrdSide.BUY if side == "BUY" else self._TrdSide.SELL

        # Lot-size enforcement for CN A-shares + HK.
        # HK lot size queried LIVE from Moomoo snapshot — the static HK_LOT_SIZES
        # table was wrong for ETFs (02822=200, 03037=500 not 100). 2026-05-22.
        # Also caps qty to available local-currency cash to avoid "Insufficient
        # Buying Power" rejections (broker rejects after submit, costs latency).
        if market in ("CN", "HK"):
            from market_calendar import china_lot_size, hk_lot_size
            live_cash_local = None
            if market == "CN":
                lot = china_lot_size(symbol)
            else:
                # Try Moomoo live snapshot first (correct lot_size), fall back to static
                lot = hk_lot_size(symbol)
                try:
                    with ft.OpenQuoteContext(host=self.host, port=self.port) as qctx:
                        ret, snap = qctx.get_market_snapshot([futu_code])
                        if ret == ft.RET_OK and not snap.empty:
                            live_lot = int(snap.iloc[0].get("lot_size", lot) or lot)
                            if live_lot > 0:
                                lot = live_lot
                except Exception as _e:
                    logger.debug(f"[Futu] live lot_size query failed for {symbol}: {_e}")
                # Pre-check local cash: query Moomoo HK cash directly so we can
                # cap the order to what's actually affordable in HKD.
                try:
                    cur_ccy = ft.Currency.HKD if market == "HK" else ft.Currency.CNH
                    with self._trade_ctx(market) as _aectx:
                        a_ret, a_info = _aectx.accinfo_query(
                            trd_env=self._TrdEnv, refresh_cache=True, currency=cur_ccy
                        )
                        if a_ret == ft.RET_OK and not a_info.empty:
                            live_cash_local = float(a_info.iloc[0].get("cash", 0) or 0)
                except Exception as _e:
                    logger.debug(f"[Futu] live cash query failed for {symbol}: {_e}")

            n_lots = int(quantity / lot)
            qty_int = n_lots * lot

            # Cash cap (HK only — broker's accinfo gave us local-cur cash above)
            if live_cash_local is not None and qty_int > 0 and price > 0:
                max_lots_by_cash = int((live_cash_local * 0.97) / (lot * price))  # 3% buffer for fees
                if max_lots_by_cash < n_lots:
                    old = qty_int
                    n_lots = max_lots_by_cash
                    qty_int = n_lots * lot
                    logger.info(
                        f"[Futu] {symbol} cash-cap: lowered {old} → {qty_int} shares "
                        f"(cash {live_cash_local:.0f} {market} cur, lot {lot}, price {price})"
                    )

            if qty_int < lot:
                return {
                    "success": False, "order_id": None,
                    "error": (f"qty {quantity:.2f} → {qty_int} shares (< 1 lot of {lot}); "
                              f"{market} {symbol}: cash {live_cash_local} insufficient or "
                              f"position too small. Deposit more {('HKD' if market=='HK' else 'CNY')}, "
                              f"or raise weight.")
                }
        else:
            qty_int = round(quantity, 4)

        acc_id_map = {
            "CN": self._cn_acc_id,
            "HK": self._hk_acc_id,
            "US": self._us_acc_id,
        }
        acc_id = acc_id_map.get(market, 0)

        try:
            with self._trade_ctx(market) as ctx:
                # Moomoo HK/CN REAL trading requires unlock_trade() with trading
                # password before each session. 2026-05-22: without this the
                # broker rejects with "Trade is not unlocked." for ALL HK orders.
                # SIMULATE env doesn't need unlock.
                if self._TrdEnv == ft.TrdEnv.REAL:
                    pwd = self._trade_password
                    if pwd:
                        unlock_ret, unlock_data = ctx.unlock_trade(pwd)
                        if unlock_ret != ft.RET_OK:
                            logger.error(f"[Futu] unlock_trade failed: {unlock_data}")
                            return {"success": False, "order_id": None,
                                    "error": f"unlock_trade failed: {unlock_data}"}
                    else:
                        logger.warning("[Futu] no trade_password set (futu_unlock_password); REAL HK/CN orders will be rejected")

                # For CN/HK market orders, use AUCTION_LIMIT or MARKET type
                order_type = self._OrderType.MARKET
                # Futu CN market orders use price=0 + MARKET order type
                order_price = 0.0 if market == "CN" else round(price, 3)

                ret, data = ctx.place_order(
                    price=order_price,
                    qty=qty_int,
                    code=futu_code,
                    trd_side=trd_side,
                    order_type=order_type,
                    trd_env=self._TrdEnv,
                    acc_id=acc_id or 0,
                )
                if ret == ft.RET_OK and not data.empty:
                    order_id = str(data.iloc[0].get("order_id", ""))
                    logger.info(f"[Futu] {side} {qty_int} {futu_code} submitted: order_id={order_id}")
                    return {"success": True, "order_id": order_id, "error": None}
                else:
                    err_msg = str(data) if ret != ft.RET_OK else "empty response"
                    logger.error(f"[Futu] Order failed: {err_msg}")
                    return {"success": False, "order_id": None, "error": err_msg}
        except Exception as e:
            logger.error(f"[Futu] _place_order exception: {e}")
            return {"success": False, "order_id": None, "error": str(e)}

    def get_position(self, symbol: str) -> Optional[Dict]:
        positions = self.get_all_positions()
        for p in positions:
            if p["symbol"] == symbol:
                return p
        return None

    def get_all_positions(self) -> List[Dict]:
        if not self._futu_available:
            return []
        ft = self._ft
        result = []
        acc_id_map = {
            "CN": self._cn_acc_id,
            "HK": self._hk_acc_id,
            "US": self._us_acc_id,
        }
        for market, acc_id in acc_id_map.items():
            try:
                with self._trade_ctx(market) as ctx:
                    ret, data = ctx.position_list_query(
                        trd_env=self._TrdEnv, acc_id=acc_id or 0
                    )
                    if ret == ft.RET_OK and not data.empty:
                        for _, row in data.iterrows():
                            futu_code = str(row.get("code", ""))
                            # Convert back: SH.600519 → 600519.SH
                            sym = _futu_to_alphatrader(futu_code)
                            qty = float(row.get("qty", 0))
                            if qty == 0:
                                continue
                            result.append({
                                "symbol": sym,
                                "quantity": qty,
                                "avg_cost": float(row.get("cost_price", 0)),
                                "current_price": float(row.get("nominal_price", 0)),
                                "unrealized_pnl": float(row.get("unrealized_pl", 0)),
                                "broker": "Futu",
                                "market": market,
                            })
            except Exception as e:
                logger.debug(f"[Futu] get_all_positions for {market}: {e}")
        return result

    # ── Extra: CN-specific helper ─────────────────────────────────────────────

    def get_cn_account_info(self) -> Optional[Dict]:
        """Return CN account details: cash_available, market_val, etc."""
        if not self._futu_available:
            return None
        ft = self._ft
        try:
            with self._trade_ctx("CN") as ctx:
                ret, data = ctx.accinfo_query(
                    trd_env=self._TrdEnv, acc_id=self._cn_acc_id or 0
                )
                if ret == ft.RET_OK and not data.empty:
                    row = data.iloc[0]
                    return {
                        "cash": float(row.get("cash", 0)),
                        "buying_power": float(row.get("avl_withdrawal_cash", 0)),
                        "total_assets": float(row.get("total_assets", 0)),
                        "market_value": float(row.get("market_val", 0)),
                        "currency": "CNY",
                    }
        except Exception as e:
            logger.error(f"[Futu] get_cn_account_info: {e}")
        return None


# ── Reverse code conversion ───────────────────────────────────────────────────

def _futu_to_alphatrader(futu_code: str) -> str:
    """
    Convert Futu code to SerenityTrader symbol.
      SH.600519  →  600519.SH
      HK.00700   →  0700.HK   (strip leading zeros for HK)
      US.AAPL    →  AAPL
    """
    if "." not in futu_code:
        return futu_code
    market, code = futu_code.split(".", 1)
    market = market.upper()
    if market in ("SH", "SZ"):
        return f"{code}.{market}"
    if market == "HK":
        # Remove leading zeros: 00700 → 700, but keep 4-digit if relevant
        stripped = code.lstrip("0") or "0"
        return f"{stripped}.HK"
    if market == "US":
        return code
    return f"{code}.{market}"


# ── Factory helper used by TradingEngine ─────────────────────────────────────

def create_futu_broker_from_settings(settings: dict) -> FutuBroker:
    """Build a FutuBroker from a Settings dict.

    `futu_security_firm` MUST match where the user opened the account:
      • FUTUSECURITIES — Futu HK (default for legacy users)
      • FUTUAU         — Moomoo Australia / Moomoo NZ
      • FUTUSG / FUTUINC / FUTUJP / FUTUMY / FUTUCA — other regional entities
    Without the right value, OpenD returns the SIMULATE account but hides the
    REAL one. Settings key is optional; defaults to FUTUSECURITIES for back-compat.
    """
    return FutuBroker(
        host=settings.get("futu_host", "127.0.0.1"),
        port=int(settings.get("futu_port", "11111")),
        trade_env=settings.get("futu_trade_env", "SIMULATE"),
        cn_acc_id=int(settings.get("futu_cn_acc_id", "0") or 0),
        hk_acc_id=int(settings.get("futu_hk_acc_id", "0") or 0),
        us_acc_id=int(settings.get("futu_us_acc_id", "0") or 0),
        security_firm=settings.get("futu_security_firm", "FUTUSECURITIES"),
        trade_password=settings.get("futu_unlock_password", ""),
    )
