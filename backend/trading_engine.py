"""
Trading engine – unified order routing for Alpaca (US), Futu (CN/HK/US),
Interactive Brokers (global), and Paper trading fallback.

Broker selection per symbol:
  ┌──────────────────────┬──────────────────────────────────────────┐
  │ Market               │ Priority                                 │
  ├──────────────────────┼──────────────────────────────────────────┤
  │ US (no suffix)       │ Alpaca → IBKR → Paper                   │
  │ CN (.SH/.SZ)         │ Futu   → Paper                          │
  │ HK (.HK)             │ Futu   → IBKR → Paper                   │
  │ JP/EU/AU/KR/SG/…     │ IBKR   → Paper                          │
  └──────────────────────┴──────────────────────────────────────────┘
"""
from __future__ import annotations

import logging
from datetime import datetime, date
from typing import Optional, Dict, List

from sqlalchemy.orm import Session
from database import Trade, Position, get_setting
import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import APIError
import position_sizer as ps
from market_calendar import detect_market, get_currency, is_china_ashare, round_to_lot

logger = logging.getLogger(__name__)


# ── Broker registry helpers ───────────────────────────────────────────────────

def _load_futu_broker(db: Session, user_id: int):
    """Lazily construct FutuBroker from user settings. Returns None if disabled."""
    futu_enabled = get_setting(db, "futu_enabled", user_id, "false")
    if futu_enabled != "true":
        return None
    try:
        from futu_broker import create_futu_broker_from_settings
        settings = _get_user_settings_dict(db, user_id)
        broker = create_futu_broker_from_settings(settings)
        return broker
    except Exception as e:
        logger.warning(f"[TradingEngine] Futu broker init failed: {e}")
        return None


def _load_ibkr_broker(db: Session, user_id: int):
    """Lazily construct IBKRBroker from user settings. Returns None if disabled."""
    ibkr_enabled = get_setting(db, "ibkr_enabled", user_id, "false")
    if ibkr_enabled != "true":
        return None
    try:
        from ibkr_broker import create_ibkr_broker_from_settings
        settings = _get_user_settings_dict(db, user_id)
        broker = create_ibkr_broker_from_settings(settings)
        return broker
    except Exception as e:
        logger.warning(f"[TradingEngine] IBKR broker init failed: {e}")
        return None


def _get_user_settings_dict(db: Session, user_id: int) -> Dict:
    """Load all user settings into a plain dict."""
    from database import Settings
    rows = db.query(Settings).filter(Settings.user_id == user_id).all()
    return {r.key: r.value for r in rows}


# ─────────────────────────────────────────────────────────────────────────────
# TradingEngine
# ─────────────────────────────────────────────────────────────────────────────

class TradingEngine:
    def __init__(self, db: Session, user_id: int):
        self.db = db
        self.user_id = user_id

        # ── Alpaca (US) ──────────────────────────────────────────────────────
        oauth_token = get_setting(db, "alpaca_oauth_token", user_id, "")
        alpaca_key = get_setting(db, "alpaca_api_key", user_id, "")
        alpaca_secret = get_setting(db, "alpaca_secret_key", user_id, "")
        alpaca_paper = get_setting(db, "alpaca_paper_mode", user_id, "true") == "true"

        self.use_alpaca = bool(oauth_token or (alpaca_key and alpaca_secret))
        if self.use_alpaca:
            base_url = "https://paper-api.alpaca.markets" if alpaca_paper else "https://api.alpaca.markets"
            if oauth_token:
                self.alpaca = tradeapi.REST(oauth=oauth_token)
            else:
                self.alpaca = tradeapi.REST(alpaca_key, alpaca_secret, base_url, api_version="v2")
        else:
            self.alpaca = None

        # ── Futu (CN / HK / US) ─────────────────────────────────────────────
        self._futu = _load_futu_broker(db, user_id)

        # ── IBKR (global) ───────────────────────────────────────────────────
        self._ibkr = _load_ibkr_broker(db, user_id)

    # ── Broker selection ─────────────────────────────────────────────────────

    def _get_broker_for_symbol(self, symbol: str):
        """
        Return (broker_obj_or_None, broker_name) for this symbol.
        broker_obj_or_None is None when using Alpaca (handled separately).
        broker_name is 'Alpaca' | 'Futu' | 'IBKR' | 'Paper'.
        """
        market = detect_market(symbol)

        if market == "US":
            if self.use_alpaca:
                return None, "Alpaca"
            if self._futu and self._futu.is_connected():
                return self._futu, "Futu"
            if self._ibkr and self._ibkr.is_connected():
                return self._ibkr, "IBKR"
            return None, "Paper"

        if market == "CN":
            if self._futu and self._futu.is_connected():
                return self._futu, "Futu"
            return None, "Paper"

        if market == "HK":
            if self._futu and self._futu.is_connected():
                return self._futu, "Futu"
            if self._ibkr and self._ibkr.is_connected():
                return self._ibkr, "IBKR"
            return None, "Paper"

        # All other international markets → IBKR or Paper
        if self._ibkr and self._ibkr.is_connected():
            return self._ibkr, "IBKR"
        return None, "Paper"

    # ── China T+1 enforcement ────────────────────────────────────────────────

    def _check_china_t1_sell(self, symbol: str) -> Optional[str]:
        """
        Enforce China T+1 rule: cannot sell same-day purchased A-shares.
        Returns error message string if blocked, else None.
        """
        if not is_china_ashare(symbol):
            return None
        today = date.today()
        recent_buy = (
            self.db.query(Trade)
            .filter(
                Trade.user_id == self.user_id,
                Trade.symbol == symbol,
                Trade.side == "BUY",
            )
            .order_by(Trade.timestamp.desc())
            .first()
        )
        if recent_buy and recent_buy.timestamp.date() >= today:
            return (
                f"[T+1规则] {symbol} 今日已买入，A股T+1制度不允许当日卖出。"
                f"最早可在 {today} 次日卖出。"
            )
        return None

    # ── Cash / position helpers (unchanged from original) ────────────────────

    def get_cash_balance(self) -> float:
        if self.use_alpaca:
            try:
                account = self.alpaca.get_account()
                return float(account.buying_power)
            except Exception as e:
                logger.error(f"Alpaca get_account error: {e}")
                return 0.0
        from database import User
        user = self.db.query(User).filter(User.id == self.user_id).first()
        return user.balance if user else 0.0

    def set_cash_balance(self, amount: float):
        if self.use_alpaca:
            return
        from database import User
        user = self.db.query(User).filter(User.id == self.user_id).first()
        if user:
            user.balance = amount
            self.db.commit()

    def get_position(self, symbol: str) -> Optional[Position]:
        return self.db.query(Position).filter(
            Position.user_id == self.user_id, Position.symbol == symbol
        ).first()

    def get_all_positions(self) -> List:
        return self.db.query(Position).filter(
            Position.user_id == self.user_id, Position.quantity != 0
        ).all()

    def sync_positions_from_alpaca(self) -> int:
        """Overwrite local DB positions with Alpaca reality."""
        if not self.use_alpaca or not self.alpaca:
            return 0
        try:
            alpaca_positions = self.alpaca.list_positions()
            alpaca_by_symbol = {p.symbol: p for p in alpaca_positions}

            for symbol, ap in alpaca_by_symbol.items():
                qty = float(ap.qty)
                avg = float(ap.avg_entry_price)
                curr = float(ap.current_price)
                unrl = float(ap.unrealized_pl)
                pos = self.db.query(Position).filter(
                    Position.user_id == self.user_id, Position.symbol == symbol
                ).first()
                if pos:
                    pos.quantity = qty
                    pos.avg_cost = avg
                    pos.current_price = curr
                    pos.unrealized_pnl = unrl
                    pos.last_updated = datetime.utcnow()
                else:
                    self.db.add(Position(
                        user_id=self.user_id, symbol=symbol,
                        quantity=qty, avg_cost=avg,
                        current_price=curr, unrealized_pnl=unrl,
                    ))

            for pos in self.db.query(Position).filter(
                Position.user_id == self.user_id, Position.quantity != 0
            ).all():
                if pos.symbol not in alpaca_by_symbol:
                    pos.quantity = 0
                    pos.unrealized_pnl = 0
                    pos.last_updated = datetime.utcnow()

            self.db.commit()
            return len(alpaca_by_symbol)
        except Exception as e:
            logger.error(f"[SyncPositions] Error: {e}")
            return 0

    def sync_positions_from_futu(self) -> int:
        """Sync CN/HK positions from Futu into local DB."""
        if not self._futu:
            return 0
        count = 0
        try:
            futu_positions = self._futu.get_all_positions()
            futu_by_symbol = {p["symbol"]: p for p in futu_positions}

            for symbol, fp in futu_by_symbol.items():
                qty = float(fp["quantity"])
                avg = float(fp["avg_cost"])
                curr = float(fp["current_price"])
                unrl = float(fp["unrealized_pnl"])
                pos = self.db.query(Position).filter(
                    Position.user_id == self.user_id, Position.symbol == symbol
                ).first()
                if pos:
                    pos.quantity = qty
                    pos.avg_cost = avg
                    pos.current_price = curr
                    pos.unrealized_pnl = unrl
                    pos.last_updated = datetime.utcnow()
                else:
                    self.db.add(Position(
                        user_id=self.user_id, symbol=symbol,
                        quantity=qty, avg_cost=avg,
                        current_price=curr, unrealized_pnl=unrl,
                    ))
                count += 1

            self.db.commit()
        except Exception as e:
            logger.error(f"[SyncFutu] Error: {e}")
        return count

    # ── Order execution ───────────────────────────────────────────────────────

    def execute_buy(
        self,
        symbol: str,
        quantity: float,
        price: float,
        ai_triggered: bool = False,
        confidence: float = None,
        reasoning: str = None,
    ) -> dict:
        """Execute a buy order via the appropriate broker (or paper)."""
        # Apply China lot-size rounding
        if is_china_ashare(symbol):
            quantity = float(round_to_lot(quantity, symbol))
            if quantity < 100:
                return {"success": False, "error": f"A股最小买入100股, 计算得 {quantity} 股不足"}

        quantity = round(quantity, 4)
        total_cost = quantity * price
        cash = self.get_cash_balance()
        position = self.get_position(symbol)
        is_cover = position and position.quantity < 0

        broker, broker_name = self._get_broker_for_symbol(symbol)
        currency = get_currency(symbol)

        # ── Route to Alpaca ──────────────────────────────────────────────────
        if broker_name == "Alpaca":
            try:
                notional_amount = round(total_cost, 2)
                if notional_amount < 1.0:
                    return {"success": False, "error": f"Notional ${notional_amount:.2f} below Alpaca minimum $1"}
                qty_amount = round(notional_amount / price, 6) if price > 0 else quantity
                order = self.alpaca.submit_order(
                    symbol=symbol, qty=qty_amount,
                    side="buy", type="market", time_in_force="day"
                )
                logger.info(f"Alpaca BUY qty={qty_amount} (~${notional_amount:.2f}) submitted: {order.id}")
            except Exception as e:
                logger.error(f"Alpaca BUY error: {e}")
                return {"success": False, "error": f"Alpaca Error: {str(e)}"}

        # ── Route to Futu / IBKR ────────────────────────────────────────────
        elif broker_name in ("Futu", "IBKR"):
            result = broker.submit_buy(symbol, quantity, price)
            if not result["success"]:
                return {"success": False, "error": result["error"]}
            logger.info(f"[{broker_name}] BUY {quantity} {symbol} @ {currency}{price:.4f} submitted")

        else:
            # Paper trading: deduct cash locally
            total_equity = cash
            for p in self.get_all_positions():
                total_equity += p.quantity * p.current_price
            if not is_cover and total_cost > (cash + total_equity):
                return {"success": False, "error": f"Insufficient funds. Available: ${total_equity:.2f}"}
            self.set_cash_balance(cash - total_cost)

        # ── Update local position DB ─────────────────────────────────────────
        if position:
            if position.quantity >= 0:
                total_val = position.quantity * position.avg_cost + total_cost
                position.avg_cost = total_val / (position.quantity + quantity)
            position.quantity += quantity
            position.current_price = price
            position.last_updated = datetime.utcnow()
            if abs(position.quantity) < 0.0001:
                position.quantity = 0
        else:
            position = Position(
                user_id=self.user_id, symbol=symbol,
                quantity=quantity, avg_cost=price,
                current_price=price, unrealized_pnl=0,
            )
            self.db.add(position)

        trade = Trade(
            user_id=self.user_id, symbol=symbol,
            side="BUY" if not is_cover else "COVER",
            quantity=quantity, price=price, total_value=total_cost,
            order_type=broker_name,
            ai_triggered=ai_triggered, ai_confidence=confidence, reasoning=reasoning,
        )
        self.db.add(trade)
        self.db.commit()

        logger.info(f"BUY executed: {quantity} {symbol} @ {currency}{price:.4f}, total {currency}{total_cost:.2f} via {broker_name}")
        return {
            "success": True,
            "trade": {
                "symbol": symbol,
                "side": "BUY" if not is_cover else "COVER",
                "quantity": quantity, "price": price,
                "total_value": total_cost,
                "currency": currency,
                "broker": broker_name,
                "timestamp": datetime.utcnow().isoformat(),
            }
        }

    def execute_sell(
        self,
        symbol: str,
        quantity: float,
        price: float,
        ai_triggered: bool = False,
        confidence: float = None,
        reasoning: str = None,
    ) -> dict:
        """Execute a sell order via the appropriate broker (or paper)."""
        # T+1 check for China A-shares
        t1_error = self._check_china_t1_sell(symbol)
        if t1_error:
            return {"success": False, "skipped": True, "reason": t1_error}

        # Apply China lot-size rounding for partial sells
        if is_china_ashare(symbol):
            quantity = float(round_to_lot(quantity, symbol))
            if quantity < 100:
                quantity = 100.0  # minimum sell unit

        quantity = round(quantity, 4)
        position = self.get_position(symbol)
        total_proceeds = quantity * price
        cash = self.get_cash_balance()
        is_short = not position or position.quantity <= 0
        currency = get_currency(symbol)

        broker, broker_name = self._get_broker_for_symbol(symbol)

        # ── Route to Alpaca ──────────────────────────────────────────────────
        if broker_name == "Alpaca":
            try:
                try:
                    alpaca_pos = self.alpaca.get_position(symbol)
                    alpaca_qty = float(alpaca_pos.qty)
                    if alpaca_qty <= 0:
                        return {"success": False, "skipped": True, "reason": f"No Alpaca position in {symbol}"}
                    quantity = min(quantity, alpaca_qty)
                except Exception:
                    return {"success": False, "skipped": True, "reason": f"No Alpaca position in {symbol} (cannot short)"}
                qty_val = int(quantity) if float(quantity).is_integer() else quantity
                order = self.alpaca.submit_order(
                    symbol=symbol, qty=qty_val,
                    side="sell", type="market", time_in_force="day"
                )
                logger.info(f"Alpaca SELL submitted: {order.id}")
            except Exception as e:
                logger.error(f"Alpaca SELL error: {e}")
                return {"success": False, "error": f"Alpaca Error: {str(e)}"}

        # ── Route to Futu / IBKR ────────────────────────────────────────────
        elif broker_name in ("Futu", "IBKR"):
            # Verify we actually hold this position
            if not position or position.quantity <= 0:
                return {"success": False, "skipped": True,
                        "reason": f"No position in {symbol} to sell via {broker_name}"}
            quantity = min(quantity, position.quantity)
            result = broker.submit_sell(symbol, quantity, price)
            if not result["success"]:
                return {"success": False, "error": result["error"]}
            logger.info(f"[{broker_name}] SELL {quantity} {symbol} @ {currency}{price:.4f} submitted")

        else:
            # Paper: add cash locally
            total_equity = cash
            for p in self.get_all_positions():
                total_equity += p.quantity * p.current_price
            if is_short and total_proceeds > (cash + total_equity):
                return {"success": False, "error": f"Margin Limit for Short. Equity: ${total_equity:.2f}"}
            self.set_cash_balance(cash + total_proceeds)

        realized_pnl = 0.0
        if position and position.quantity > 0:
            realized_pnl = (price - position.avg_cost) * min(quantity, position.quantity)

        # ── Update local position DB ─────────────────────────────────────────
        if position:
            if position.quantity <= 0:
                total_val = abs(position.quantity) * position.avg_cost + total_proceeds
                position.avg_cost = total_val / (abs(position.quantity) + quantity)
            position.quantity -= quantity
            position.current_price = price
            position.last_updated = datetime.utcnow()
            if abs(position.quantity) < 0.0001:
                position.quantity = 0
        else:
            allow_short = get_setting(self.db, "allow_short_selling", self.user_id, "false") == "true"
            if not allow_short:
                return {"success": False, "skipped": True, "reason": "No position to sell and short selling is disabled"}
            position = Position(
                user_id=self.user_id, symbol=symbol,
                quantity=-quantity, avg_cost=price,
                current_price=price, unrealized_pnl=0,
            )
            self.db.add(position)

        trade = Trade(
            user_id=self.user_id, symbol=symbol,
            side="SELL" if not is_short else "SHORT",
            quantity=quantity, price=price, total_value=total_proceeds,
            order_type=broker_name,
            ai_triggered=ai_triggered, ai_confidence=confidence, reasoning=reasoning,
        )
        self.db.add(trade)
        self.db.commit()

        logger.info(f"SELL executed: {quantity} {symbol} @ {currency}{price:.4f}, P&L {currency}{realized_pnl:.2f} via {broker_name}")
        return {
            "success": True,
            "trade": {
                "symbol": symbol,
                "side": "SELL" if not is_short else "SHORT",
                "quantity": quantity, "price": price,
                "total_value": total_proceeds,
                "realized_pnl": round(realized_pnl, 4),
                "currency": currency,
                "broker": broker_name,
                "timestamp": datetime.utcnow().isoformat(),
            }
        }

    def auto_trade(self, signal: dict, current_price: float, indicators: dict = None) -> dict:
        """Execute an auto-trade based on an AI signal with position sizing."""
        symbol = signal.get("symbol")
        action = signal.get("signal")
        confidence = signal.get("confidence", 0)
        reasoning = signal.get("reasoning", "")
        weight = signal.get("recommended_weight_pct")

        min_confidence = float(get_setting(self.db, "auto_trade_min_confidence", self.user_id, "0.75"))
        risk_per_trade_pct = float(get_setting(self.db, "risk_per_trade_pct", self.user_id, "2.0"))
        auto_trade_enabled = get_setting(self.db, "auto_trade_enabled", self.user_id, "false") == "true"

        if not auto_trade_enabled:
            return {"success": False, "skipped": True, "reason": "Auto-trading is disabled"}
        if confidence < min_confidence:
            return {"success": False, "skipped": True,
                    "reason": f"Confidence {confidence:.0%} below minimum {min_confidence:.0%}"}

        # Market-specific auto-trade gate: check market is open
        from market_calendar import is_symbol_market_open
        if not is_symbol_market_open(symbol):
            market = detect_market(symbol)
            return {"success": False, "skipped": True,
                    "reason": f"Market {market} is currently closed"}

        cash = self.get_cash_balance()
        total_equity = cash
        if not self.use_alpaca:
            for p in self.get_all_positions():
                total_equity += p.quantity * p.current_price
        else:
            try:
                total_equity = float(self.alpaca.get_account().equity)
            except Exception:
                total_equity = cash

        # For Futu CN/HK accounts, use Futu equity
        market = detect_market(symbol)
        if market in ("CN", "HK") and self._futu:
            try:
                acct = self._futu.get_account()
                if acct.get("equity", 0) > 0:
                    total_equity = acct["equity"]
                    cash = acct["cash"]
            except Exception:
                pass

        # Position sizing: Kelly → DCF weight → fixed risk%
        target_price = signal.get("target_price")
        stop_loss = signal.get("stop_loss")

        kelly_sz = None
        if (action in ("BUY", "COVER") and
                target_price and stop_loss and
                target_price > current_price and stop_loss < current_price):
            kelly_sz = ps.kelly_position_size(
                confidence=confidence,
                current_price=current_price,
                target_price=float(target_price),
                stop_loss=float(stop_loss),
                portfolio_value=total_equity,
                indicators=indicators,
            )

        if kelly_sz and not kelly_sz["skip"]:
            risk_amount = kelly_sz["dollar_amount"]
            logger.info(f"[Kelly] {symbol} sizing: {kelly_sz['reason']}")
        elif weight is not None:
            target_allocation_pct = min(200.0, abs(float(weight)) * 100)
            risk_amount = total_equity * (target_allocation_pct / 100)
        else:
            risk_amount = total_equity * (risk_per_trade_pct / 100)

        quantity = round(risk_amount / current_price, 4)

        if action in ("BUY", "COVER"):
            if quantity < 0.001:
                return {"success": False, "error": f"Calculated BUY qty too small for equity {total_equity}"}
            if action == "COVER":
                pos = self.get_position(symbol)
                if pos and pos.quantity < 0:
                    quantity = min(quantity, abs(pos.quantity))
                else:
                    return {"success": False, "skipped": True, "reason": "No short position to cover"}
            return self.execute_buy(symbol, quantity, current_price, True, confidence, reasoning)

        elif action in ("SELL", "SHORT"):
            if quantity < 0.001:
                return {"success": False, "error": "Calculated SELL qty too small"}
            if action == "SHORT":
                allow_short = get_setting(self.db, "allow_short_selling", self.user_id, "false") == "true"
                if not allow_short:
                    return {"success": False, "skipped": True, "reason": "Short selling disabled"}
                # CN A-shares: no short selling for retail
                if is_china_ashare(symbol):
                    return {"success": False, "skipped": True, "reason": "A股不支持普通账户做空"}
            if action == "SELL":
                pos = self.get_position(symbol)
                if not pos or pos.quantity <= 0:
                    return {"success": False, "skipped": True, "reason": f"No long position in {symbol} to sell"}
                quantity = min(quantity, pos.quantity)
            return self.execute_sell(symbol, quantity, current_price, True, confidence, reasoning)

        return {"success": False, "skipped": True, "reason": "Signal is HOLD"}

    # ── Price / P&L helpers ───────────────────────────────────────────────────

    def update_position_prices(self, prices: dict):
        positions = self.get_all_positions()
        for pos in positions:
            if pos.symbol in prices:
                pos.current_price = prices[pos.symbol]
                if pos.quantity < 0:
                    pos.unrealized_pnl = (pos.avg_cost - pos.current_price) * abs(pos.quantity)
                else:
                    pos.unrealized_pnl = (pos.current_price - pos.avg_cost) * pos.quantity
                pos.last_updated = datetime.utcnow()
        self.db.commit()

    def get_portfolio_summary(self) -> dict:
        """Calculate portfolio metrics aggregated across all brokers."""

        # ── Alpaca (US) ──────────────────────────────────────────────────────
        if self.use_alpaca:
            try:
                account = self.alpaca.get_account()
                positions_api = self.alpaca.list_positions()
                cash = float(account.cash)
                total_equity = float(account.equity)
                total_market_value = total_equity - cash
                initial_cash = float(get_setting(self.db, "initial_cash", self.user_id, "100000.0"))
                total_return = total_equity - initial_cash
                total_return_pct = (total_return / initial_cash * 100) if initial_cash > 0 else 0

                positions_data = []
                total_cost_basis = unrealized_pnl = 0.0

                for p in positions_api:
                    qty = float(p.qty)
                    avg_entry = float(p.avg_entry_price)
                    current_price = float(p.current_price)
                    market_val = float(p.market_value)
                    cost_basis = float(p.cost_basis)
                    unrealized = float(p.unrealized_pl)
                    unrealized_pct = float(p.unrealized_plpc) * 100
                    total_cost_basis += cost_basis
                    unrealized_pnl += unrealized
                    positions_data.append({
                        "symbol": p.symbol, "quantity": round(qty, 4),
                        "avg_cost": round(avg_entry, 2), "current_price": round(current_price, 2),
                        "market_value": round(market_val, 2), "cost_basis": round(cost_basis, 2),
                        "unrealized_pnl": round(unrealized, 2), "unrealized_pnl_pct": round(unrealized_pct, 2),
                        "weight_pct": round((abs(market_val) / total_equity * 100), 2) if total_equity > 0 else 0,
                        "currency": "USD", "broker": "Alpaca",
                    })

                all_trades = self.db.query(Trade).filter(Trade.user_id == self.user_id).all()
                return {
                    "cash": round(cash, 2), "total_market_value": round(total_market_value, 2),
                    "total_equity": round(total_equity, 2), "total_cost_basis": round(total_cost_basis, 2),
                    "unrealized_pnl": round(unrealized_pnl, 2), "total_return": round(total_return, 2),
                    "total_return_pct": round(total_return_pct, 2), "initial_cash": round(initial_cash, 2),
                    "total_trades": len(all_trades), "positions": positions_data, "provider": "Alpaca",
                }
            except Exception as e:
                logger.error(f"Alpaca get_portfolio_summary Error: {e}")

        # ── Paper / Local DB fallback ─────────────────────────────────────────
        positions = self.get_all_positions()
        cash = self.get_cash_balance()
        initial_cash = float(get_setting(self.db, "initial_cash", self.user_id, "100000.0"))
        total_market_value = sum(p.quantity * p.current_price for p in positions)
        total_cost_basis = sum(abs(p.quantity) * p.avg_cost for p in positions)
        unrealized_pnl = total_equity = cash = cash

        for p in positions:
            total_equity += p.quantity * p.current_price
            if p.quantity < 0:
                unrealized_pnl += (p.avg_cost - p.current_price) * abs(p.quantity)
            else:
                unrealized_pnl += (p.current_price - p.avg_cost) * p.quantity

        total_return = total_equity - initial_cash
        total_return_pct = (total_return / initial_cash * 100) if initial_cash > 0 else 0
        all_trades = self.db.query(Trade).filter(Trade.user_id == self.user_id).all()

        positions_data = []
        for p in positions:
            if abs(p.quantity) > 0:
                if p.quantity < 0:
                    unrealized = (p.avg_cost - p.current_price) * abs(p.quantity)
                    unrealized_pct = ((p.avg_cost - p.current_price) / p.avg_cost * 100) if p.avg_cost > 0 else 0
                else:
                    unrealized = (p.current_price - p.avg_cost) * p.quantity
                    unrealized_pct = ((p.current_price - p.avg_cost) / p.avg_cost * 100) if p.avg_cost > 0 else 0
                positions_data.append({
                    "symbol": p.symbol, "quantity": round(p.quantity, 4),
                    "avg_cost": round(p.avg_cost, 4), "current_price": round(p.current_price, 4),
                    "market_value": round(p.quantity * p.current_price, 4),
                    "cost_basis": round(abs(p.quantity) * p.avg_cost, 4),
                    "unrealized_pnl": round(unrealized, 4), "unrealized_pnl_pct": round(unrealized_pct, 2),
                    "weight_pct": round((abs(p.quantity) * p.current_price / total_equity * 100), 2) if total_equity > 0 else 0,
                    "currency": get_currency(p.symbol),
                    "broker": "Paper",
                })

        broker_label = "Paper"
        if self._futu and self._futu.is_connected():
            broker_label = "Futu (Paper+Live)"
        elif self._ibkr and self._ibkr.is_connected():
            broker_label = "IBKR (Paper+Live)"

        return {
            "cash": round(cash, 2), "total_market_value": round(total_market_value, 2),
            "total_equity": round(total_equity, 2), "total_cost_basis": round(total_cost_basis, 2),
            "unrealized_pnl": round(unrealized_pnl, 2), "total_return": round(total_return, 2),
            "total_return_pct": round(total_return_pct, 2), "initial_cash": round(initial_cash, 2),
            "total_trades": len(all_trades), "positions": positions_data, "provider": broker_label,
        }
