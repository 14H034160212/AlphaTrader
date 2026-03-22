"""
Abstract broker interface for multi-market trading.
All brokers (Alpaca, Futu, IBKR, Paper) implement this interface.
"""
from abc import ABC, abstractmethod
from typing import Optional, Dict, List


class BrokerInterface(ABC):
    """Abstract base class for all trading brokers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Broker name, e.g. 'Alpaca', 'Futu', 'IBKR', 'Paper'."""

    @property
    @abstractmethod
    def supported_markets(self) -> List[str]:
        """
        List of market codes this broker can *execute* on.
        E.g. ['US'], ['CN', 'HK', 'US'], ['US','GB','DE','JP', ...]
        """

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if the broker API / daemon is reachable."""

    @abstractmethod
    def get_account(self) -> Dict:
        """
        Return account summary.
        Keys: cash, equity, buying_power, currency.
        """

    @abstractmethod
    def submit_buy(self, symbol: str, quantity: float, price: float) -> Dict:
        """
        Submit a market buy order.
        Returns: {success: bool, order_id: str|None, error: str|None}
        """

    @abstractmethod
    def submit_sell(self, symbol: str, quantity: float, price: float) -> Dict:
        """
        Submit a market sell order.
        Returns: {success: bool, order_id: str|None, error: str|None}
        """

    @abstractmethod
    def get_position(self, symbol: str) -> Optional[Dict]:
        """
        Return current position for symbol or None.
        Keys: symbol, quantity, avg_cost, current_price, unrealized_pnl.
        """

    @abstractmethod
    def get_all_positions(self) -> List[Dict]:
        """Return all open positions as list of dicts (same keys as get_position)."""


class PaperBroker(BrokerInterface):
    """
    Pure paper-trading broker – all execution is simulated locally.
    Used as fallback when a real broker is not configured/reachable.
    This class holds no state; position state lives in the SQLite DB.
    """

    @property
    def name(self) -> str:
        return "Paper"

    @property
    def supported_markets(self) -> List[str]:
        # Paper trading can simulate any market
        return ["US", "CN", "HK", "JP", "GB", "DE", "FR", "NL", "IT",
                "AU", "KR", "SG", "IN", "BR", "CA", "MX", "INTL"]

    def is_connected(self) -> bool:
        return True  # Always "connected" – it's local simulation

    def get_account(self) -> Dict:
        return {"cash": 0.0, "equity": 0.0, "buying_power": 0.0, "currency": "USD"}

    def submit_buy(self, symbol: str, quantity: float, price: float) -> Dict:
        return {"success": True, "order_id": "paper", "error": None}

    def submit_sell(self, symbol: str, quantity: float, price: float) -> Dict:
        return {"success": True, "order_id": "paper", "error": None}

    def get_position(self, symbol: str) -> Optional[Dict]:
        return None  # Positions are read from DB by TradingEngine

    def get_all_positions(self) -> List[Dict]:
        return []
