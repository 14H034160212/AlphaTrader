import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.append(str(Path(__file__).resolve().parents[1]))

import trading_engine as te


class TradingEngineGoldBlockTests(unittest.TestCase):
    def test_gold_symbols_are_blocked_from_buy_execution(self):
        engine = te.TradingEngine.__new__(te.TradingEngine)
        engine.db = object()
        engine.user_id = 1
        engine.use_alpaca = False
        engine.get_cash_balance = lambda: 100000.0
        engine.get_all_positions = lambda: []

        with patch.object(te, "get_setting", side_effect=lambda *args, **kwargs: "0.75" if args[1] == "auto_trade_min_confidence" else "true" if args[1] == "auto_trade_enabled" else "2.0"), \
             patch("market_calendar.is_symbol_market_open", return_value=True):
            result = engine.auto_trade(
                {"symbol": "GLD", "signal": "BUY", "confidence": 0.95, "reasoning": "gold thesis"},
                current_price=200.0,
            )

        self.assertFalse(result.get("success", False))
        self.assertTrue(result.get("skipped", False))
        self.assertIn("gold", (result.get("reason") or "").lower())


if __name__ == "__main__":
    unittest.main()
