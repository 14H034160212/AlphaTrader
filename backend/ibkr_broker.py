"""
Interactive Brokers (IBKR) broker integration via ib_insync.

Supports virtually all global markets:
  US, UK, DE, FR, NL, IT, ES, JP, HK, AU, KR, SG, CA, IN, BR, and many more.

Prerequisites (set in Settings):
  • ibkr_host       (default: 127.0.0.1)
  • ibkr_port       (default: 7497 = TWS paper / 4001 = Gateway paper)
                   Live TWS: 7496 | Live Gateway: 4001
  • ibkr_client_id  (default: 10)
  • ibkr_account    (optional; picks primary account if blank)

Install:
  pip install ib_insync

Run TWS / IB Gateway before connecting.
"""
from __future__ import annotations

import logging
import time as _time
from typing import Optional, Dict, List

from broker_interface import BrokerInterface
from market_calendar import detect_market, MARKET_CURRENCIES

logger = logging.getLogger(__name__)

# ── Exchange & currency mappings for IBKR ─────────────────────────────────────
# IBKR uses its own exchange names (SMART = auto-route, TSE = Tokyo, etc.)
_MARKET_TO_IBKR_EXCHANGE: Dict[str, str] = {
    "US": "SMART",
    "CN": "SEHKSZSE",    # IBKR routes CN via Stock Connect (limited)
    "HK": "SEHK",
    "JP": "TSE",
    "GB": "LSE",
    "DE": "XETRA",
    "FR": "SBF",
    "NL": "AEB",
    "IT": "BVME",
    "ES": "BM",
    "CH": "EBS",
    "AU": "ASX",
    "KR": "KSE",
    "SG": "SGX",
    "IN": "NSE",
    "BR": "BOVESPA",
    "CA": "TSX",
    "MX": "MEXI",
    "TW": "TSE",         # Taiwan
    "ZA": "JSE",
    "TH": "SET",
    "MY": "BURSA",
    "TR": "BIST",
    "SA": "TADAWUL",
    "IL": "TASE",
    "AR": "BCBA",
    "RU": "MOEX",
}


def _build_ibkr_contract(symbol: str):
    """
    Build an ib_insync Stock contract from an AlphaTrader symbol.
    Returns an ib_insync.Stock (or None if ib_insync not available).
    """
    try:
        from ib_insync import Stock, Forex
    except ImportError:
        return None

    market = detect_market(symbol)
    currency = MARKET_CURRENCIES.get(market, "USD")
    exchange = _MARKET_TO_IBKR_EXCHANGE.get(market, "SMART")

    # Strip exchange suffix for IBKR ticker format
    if "." in symbol:
        ticker = symbol.rsplit(".", 1)[0]
    else:
        ticker = symbol

    # HK stocks: IBKR uses numeric code without leading zeros
    if market == "HK":
        ticker = ticker.lstrip("0") or "0"

    return Stock(ticker, exchange, currency)


# ── IBKR Broker ───────────────────────────────────────────────────────────────

class IBKRBroker(BrokerInterface):
    """
    Interactive Brokers broker via ib_insync.
    Gracefully degrades to paper fallback if ib_insync is not installed
    or TWS/Gateway is not running.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 10,
        account: str = "",
        readonly: bool = False,
    ):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.account = account
        self.readonly = readonly

        self._ib_available = False
        self._ib_module = None

        try:
            import ib_insync
            self._ib_module = ib_insync
            self._ib_available = True
            logger.info(f"[IBKR] ib_insync loaded. Target: {host}:{port} clientId={client_id}")
        except ImportError:
            logger.warning("[IBKR] ib_insync not installed. Run: pip install ib_insync. Using paper fallback.")

    # ── Connection context manager ────────────────────────────────────────────

    def _connect(self):
        """Create a new IB connection. Caller must call ib.disconnect()."""
        ib = self._ib_module.IB()
        ib.connect(
            host=self.host,
            port=self.port,
            clientId=self.client_id,
            readonly=self.readonly,
            timeout=10,
        )
        return ib

    # ── BrokerInterface ──────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "IBKR"

    @property
    def supported_markets(self) -> List[str]:
        # IBKR supports most global markets
        return [
            "US", "HK", "JP", "GB", "DE", "FR", "NL", "IT", "ES", "CH",
            "AU", "KR", "SG", "CA", "IN", "BR", "MX", "TW", "ZA", "TH",
            "MY", "TR", "SA", "IL", "AR", "RU",
        ]

    def is_connected(self) -> bool:
        if not self._ib_available:
            return False
        try:
            ib = self._connect()
            connected = ib.isConnected()
            ib.disconnect()
            return connected
        except Exception:
            return False

    def get_account(self) -> Dict:
        if not self._ib_available:
            return {"cash": 0.0, "equity": 0.0, "buying_power": 0.0, "currency": "USD"}
        ib = None
        try:
            ib = self._connect()
            acct = self.account or ""
            summary = ib.accountSummary(acct)
            data = {item.tag: item.value for item in summary if item.currency in ("USD", "BASE", "")}
            cash = float(data.get("CashBalance", data.get("TotalCashValue", 0)))
            equity = float(data.get("NetLiquidation", cash))
            buying_power = float(data.get("BuyingPower", cash))
            return {
                "cash": cash,
                "equity": equity,
                "buying_power": buying_power,
                "currency": "USD",
            }
        except Exception as e:
            logger.error(f"[IBKR] get_account error: {e}")
            return {"cash": 0.0, "equity": 0.0, "buying_power": 0.0, "currency": "USD"}
        finally:
            if ib:
                try:
                    ib.disconnect()
                except Exception:
                    pass

    def submit_buy(self, symbol: str, quantity: float, price: float) -> Dict:
        return self._place_order(symbol, quantity, "BUY")

    def submit_sell(self, symbol: str, quantity: float, price: float) -> Dict:
        return self._place_order(symbol, quantity, "SELL")

    def _place_order(self, symbol: str, quantity: float, action: str) -> Dict:
        if not self._ib_available:
            return {"success": False, "order_id": None, "error": "ib_insync not installed"}

        ib = None
        try:
            contract = _build_ibkr_contract(symbol)
            if contract is None:
                return {"success": False, "order_id": None, "error": "Could not build IBKR contract"}

            ib = self._connect()

            # Qualify the contract (get full details from IBKR)
            qualified = ib.qualifyContracts(contract)
            if not qualified:
                return {"success": False, "order_id": None,
                        "error": f"IBKR could not qualify contract for {symbol}"}

            # Use MarketOrder for execution
            order = self._ib_module.MarketOrder(action, round(abs(quantity), 4))
            order.account = self.account or ""

            trade = ib.placeOrder(qualified[0], order)

            # Wait briefly for order acknowledgement
            for _ in range(30):
                ib.sleep(0.1)
                if trade.orderStatus.status not in ("PreSubmitted", "Submitted", ""):
                    break

            order_id = str(trade.order.orderId)
            status = trade.orderStatus.status
            logger.info(f"[IBKR] {action} {quantity} {symbol} submitted: orderId={order_id} status={status}")

            if status in ("PreSubmitted", "Submitted", "Filled"):
                return {"success": True, "order_id": order_id, "error": None}
            else:
                return {"success": False, "order_id": order_id,
                        "error": f"Order status: {status}"}
        except Exception as e:
            logger.error(f"[IBKR] _place_order exception for {symbol}: {e}")
            return {"success": False, "order_id": None, "error": str(e)}
        finally:
            if ib:
                try:
                    ib.disconnect()
                except Exception:
                    pass

    def get_position(self, symbol: str) -> Optional[Dict]:
        positions = self.get_all_positions()
        for p in positions:
            if p["symbol"] == symbol:
                return p
        return None

    def get_all_positions(self) -> List[Dict]:
        if not self._ib_available:
            return []
        ib = None
        try:
            ib = self._connect()
            ib_positions = ib.positions(self.account or "")
            result = []
            for pos in ib_positions:
                qty = float(pos.position)
                if qty == 0:
                    continue
                contract = pos.contract
                # Reconstruct AlphaTrader symbol from IBKR contract
                ticker = contract.symbol
                exch = contract.exchange or contract.primaryExch or ""
                sym = _ibkr_to_alphatrader(ticker, exch, contract.currency)
                result.append({
                    "symbol": sym,
                    "quantity": qty,
                    "avg_cost": float(pos.avgCost) if pos.avgCost else 0.0,
                    "current_price": 0.0,  # Not available directly from position
                    "unrealized_pnl": 0.0,
                    "broker": "IBKR",
                    "market": detect_market(sym),
                })
            return result
        except Exception as e:
            logger.error(f"[IBKR] get_all_positions error: {e}")
            return []
        finally:
            if ib:
                try:
                    ib.disconnect()
                except Exception:
                    pass


# ── Reverse symbol mapping ─────────────────────────────────────────────────────

_IBKR_EXCHANGE_TO_SUFFIX: Dict[str, str] = {
    "SEHK": "HK", "TSE": "T", "LSE": "L", "XETRA": "DE",
    "SBF": "PA", "AEB": "AS", "BVME": "MI", "BM": "MC",
    "EBS": "SW", "ASX": "AX", "KSE": "KS", "SGX": "SI",
    "NSE": "NS", "BOVESPA": "SA", "TSX": "TO", "MEXI": "MX",
    "JSE": "JO", "TASE": "TA",
}


def _ibkr_to_alphatrader(ticker: str, exchange: str, currency: str) -> str:
    """Best-effort reverse map of IBKR ticker+exchange → AlphaTrader symbol."""
    if currency == "USD" and exchange in ("SMART", "NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"):
        return ticker
    suffix = _IBKR_EXCHANGE_TO_SUFFIX.get(exchange, "")
    if suffix:
        return f"{ticker}.{suffix}"
    # Fallback: ticker as-is
    return ticker


# ── Factory helper ─────────────────────────────────────────────────────────────

def create_ibkr_broker_from_settings(settings: dict) -> IBKRBroker:
    """Build an IBKRBroker from a Settings dict."""
    return IBKRBroker(
        host=settings.get("ibkr_host", "127.0.0.1"),
        port=int(settings.get("ibkr_port", "7497")),
        client_id=int(settings.get("ibkr_client_id", "10")),
        account=settings.get("ibkr_account", ""),
    )
