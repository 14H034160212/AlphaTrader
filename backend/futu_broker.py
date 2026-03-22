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
    Convert AlphaTrader symbol to Futu code format.
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
    ):
        self.host = host
        self.port = port
        self._trade_env = trade_env
        self._cn_acc_id = cn_acc_id
        self._hk_acc_id = hk_acc_id
        self._us_acc_id = us_acc_id

        self._futu_available = False
        self._TrdEnv = None
        self._TrdSide = None
        self._OrderType = None
        self._ft = None

        try:
            import futu as ft
            self._ft = ft
            self._TrdEnv = ft.TrdEnv.REAL if trade_env == "REAL" else ft.TrdEnv.SIMULATE
            self._TrdSide = ft.TrdSide
            self._OrderType = ft.OrderType
            self._futu_available = True
            logger.info(f"[Futu] futu-api loaded. OpenD target: {host}:{port} env={trade_env}")
        except ImportError:
            logger.warning("[Futu] futu-api not installed. Run: pip install futu-api. Using paper fallback.")

    # ── BrokerInterface ──────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "Futu"

    @property
    def supported_markets(self) -> List[str]:
        return ["CN", "HK", "US"]

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

    def get_account(self) -> Dict:
        if not self._futu_available:
            return {"cash": 0.0, "equity": 0.0, "buying_power": 0.0, "currency": "USD"}
        try:
            ft = self._ft
            # Try CN account first, then HK, then US
            for ctx_cls, market, acc_id, currency in [
                (ft.OpenCNTradeContext, "CN", self._cn_acc_id, "CNY"),
                (ft.OpenHKTradeContext, "HK", self._hk_acc_id, "HKD"),
                (ft.OpenUSTradeContext, "US", self._us_acc_id, "USD"),
            ]:
                try:
                    with ctx_cls(host=self.host, port=self.port) as ctx:
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

        # Minimum lot size for CN A-shares
        if is_china_ashare(symbol):
            from market_calendar import round_to_lot
            quantity = float(round_to_lot(quantity, symbol))
            if quantity < 100:
                return {"success": False, "order_id": None,
                        "error": f"CN A-share min lot is 100 shares, got {quantity}"}

        # Integer qty for HK/CN
        qty_int = int(quantity) if market in ("CN", "HK") else round(quantity, 4)

        ctx_cls_map = {
            "CN": ft.OpenCNTradeContext,
            "HK": ft.OpenHKTradeContext,
            "US": ft.OpenUSTradeContext,
        }
        acc_id_map = {
            "CN": self._cn_acc_id,
            "HK": self._hk_acc_id,
            "US": self._us_acc_id,
        }

        ctx_cls = ctx_cls_map.get(market, ft.OpenUSTradeContext)
        acc_id = acc_id_map.get(market, 0)

        try:
            with ctx_cls(host=self.host, port=self.port) as ctx:
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
        ctx_map = {
            "CN": (ft.OpenCNTradeContext, self._cn_acc_id),
            "HK": (ft.OpenHKTradeContext, self._hk_acc_id),
            "US": (ft.OpenUSTradeContext, self._us_acc_id),
        }
        for market, (ctx_cls, acc_id) in ctx_map.items():
            try:
                with ctx_cls(host=self.host, port=self.port) as ctx:
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
            with ft.OpenCNTradeContext(host=self.host, port=self.port) as ctx:
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
    Convert Futu code to AlphaTrader symbol.
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
    """Build a FutuBroker from a Settings dict."""
    return FutuBroker(
        host=settings.get("futu_host", "127.0.0.1"),
        port=int(settings.get("futu_port", "11111")),
        trade_env=settings.get("futu_trade_env", "SIMULATE"),
        cn_acc_id=int(settings.get("futu_cn_acc_id", "0") or 0),
        hk_acc_id=int(settings.get("futu_hk_acc_id", "0") or 0),
        us_acc_id=int(settings.get("futu_us_acc_id", "0") or 0),
    )
