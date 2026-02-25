"""Paper trading engine and Alpaca Live Trading engine - simulates order execution or routes to Alpaca."""
import logging
from datetime import datetime
import sys
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")

from typing import Optional
from sqlalchemy.orm import Session
from database import Trade, Position, get_setting
import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import APIError

logger = logging.getLogger(__name__)


class TradingEngine:
    def __init__(self, db: Session, user_id: int):
        self.db = db
        self.user_id = user_id
        alpaca_key = get_setting(self.db, "alpaca_api_key", self.user_id, "")
        alpaca_secret = get_setting(self.db, "alpaca_secret_key", self.user_id, "")
        alpaca_paper = get_setting(self.db, "alpaca_paper_mode", self.user_id, "true") == "true"
        
        self.use_alpaca = bool(alpaca_key and alpaca_secret)
        if self.use_alpaca:
            base_url = "https://paper-api.alpaca.markets" if alpaca_paper else "https://api.alpaca.markets"
            self.alpaca = tradeapi.REST(alpaca_key, alpaca_secret, base_url, api_version='v2')
        else:
            self.alpaca = None

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
             pass # Cannot manually set cash balance in Alpaca
        from database import User
        user = self.db.query(User).filter(User.id == self.user_id).first()
        if user:
            user.balance = amount
            self.db.commit()

    def get_position(self, symbol: str) -> Optional[Position]:
        # Local DB fallback
        return self.db.query(Position).filter(Position.user_id == self.user_id, Position.symbol == symbol).first()

    def get_all_positions(self) -> list:
        # Local DB fallback
        return self.db.query(Position).filter(Position.user_id == self.user_id, Position.quantity != 0).all()

    def execute_buy(
        self,
        symbol: str,
        quantity: float,
        price: float,
        ai_triggered: bool = False,
        confidence: float = None,
        reasoning: str = None,
    ) -> dict:
        """Execute a buy order (paper trading or Alpaca). Handles Buy-to-Cover."""
        quantity = round(quantity, 4)
        total_cost = quantity * price
        cash = self.get_cash_balance()
        position = self.get_position(symbol)

        is_cover = position and position.quantity < 0
        total_equity = cash
        
        if not self.use_alpaca:
            for p in self.get_all_positions():
                # Value of position (negative for shorts, positive for longs)
                total_equity += p.quantity * p.current_price
            
            # Allow 2x Gross Leverage for margin trading on long buys
            if not is_cover and total_cost > (cash + total_equity):
                return {"success": False, "error": f"Margin Limit Reached. Max Equity available: ${total_equity:.2f}"}

        if self.use_alpaca:
            try:
                qty_val = int(quantity) if quantity.is_integer() else quantity
                order = self.alpaca.submit_order(
                    symbol=symbol,
                    qty=qty_val,
                    side='buy',
                    type='market',
                    time_in_force='day'
                )
                logger.info(f"Alpaca BUY submitted: {order.id}")
            except Exception as e:
                logger.error(f"Alpaca API Error on BUY: {e}")
                return {"success": False, "error": f"Alpaca Error: {str(e)}"}
        else:
            # Deduct cash locally
            self.set_cash_balance(cash - total_cost)

        # Update position locally (for history and fast UI access)
        if position:
            if position.quantity < 0:
                # Buy-to-Cover short position, average cost stays the same
                pass
            else:
                total_value = position.quantity * position.avg_cost + total_cost
                position.avg_cost = total_value / (position.quantity + quantity)
            
            position.quantity += quantity
            position.current_price = price
            position.last_updated = datetime.utcnow()
            if abs(position.quantity) < 0.0001:
                position.quantity = 0
        else:
            position = Position(
                user_id=self.user_id,
                symbol=symbol,
                quantity=quantity,
                avg_cost=price,
                current_price=price,
                unrealized_pnl=0,
            )
            self.db.add(position)

        # Record trade
        market = "Alpaca" if self.use_alpaca else "Paper"
        trade = Trade(
            user_id=self.user_id,
            symbol=symbol,
            side="BUY" if not is_cover else "COVER",
            quantity=quantity,
            price=price,
            total_value=total_cost,
            order_type=market,
            ai_triggered=ai_triggered,
            ai_confidence=confidence,
            reasoning=reasoning,
        )
        self.db.add(trade)
        self.db.commit()

        logger.info(f"BUY executed: {quantity} {symbol} @ ${price:.2f}, total ${total_cost:.2f}")
        return {
            "success": True,
            "trade": {
                "symbol": symbol,
                "side": "BUY" if not is_cover else "COVER",
                "quantity": quantity,
                "price": price,
                "total_value": total_cost,
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
        """Execute a sell order (paper trading or Alpaca). Handles Short Selling."""
        quantity = round(quantity, 4)
        position = self.get_position(symbol)
        total_proceeds = quantity * price
        cash = self.get_cash_balance()
        total_equity = cash
        
        is_short = not position or position.quantity <= 0

        if not self.use_alpaca:
            for p in self.get_all_positions():
                total_equity += p.quantity * p.current_price
            
            # Margin check for short selling -> Leverage limit of 2x total equity
            if is_short:
                if total_proceeds > (cash + total_equity):
                    return {"success": False, "error": f"Margin Limit Reached for Short. Equity: ${total_equity:.2f}"}

        if self.use_alpaca:
            try:
                qty_val = int(quantity) if quantity.is_integer() else quantity
                order = self.alpaca.submit_order(
                    symbol=symbol,
                    qty=qty_val,
                    side='sell',
                    type='market',
                    time_in_force='day'
                )
                logger.info(f"Alpaca SELL submitted: {order.id}")
            except Exception as e:
                logger.error(f"Alpaca API Error on SELL: {e}")
                return {"success": False, "error": f"Alpaca Error: {str(e)}"}
        else:
            # Add cash locally (for short sale we receive cash but owe the stock)
            self.set_cash_balance(cash + total_proceeds)

        realized_pnl = 0
        if position and position.quantity > 0:
            realized_pnl = (price - position.avg_cost) * min(quantity, position.quantity)

        # Update position locally
        if position:
            if position.quantity <= 0:
                # Adding to short position
                total_value = abs(position.quantity) * position.avg_cost + total_proceeds
                position.avg_cost = total_value / (abs(position.quantity) + quantity)
                
            position.quantity -= quantity
            position.current_price = price
            position.last_updated = datetime.utcnow()
            if abs(position.quantity) < 0.0001:
                position.quantity = 0
        else:
            position = Position(
                user_id=self.user_id,
                symbol=symbol,
                quantity=-quantity,
                avg_cost=price,
                current_price=price,
                unrealized_pnl=0,
            )
            self.db.add(position)

        # Record trade
        market = "Alpaca" if self.use_alpaca else "Paper"
        trade = Trade(
            user_id=self.user_id,
            symbol=symbol,
            side="SELL" if not is_short else "SHORT",
            quantity=quantity,
            price=price,
            total_value=total_proceeds,
            order_type=market,
            ai_triggered=ai_triggered,
            ai_confidence=confidence,
            reasoning=reasoning,
        )
        self.db.add(trade)
        self.db.commit()

        logger.info(f"SELL/SHORT executed: {quantity} {symbol} @ ${price:.2f}, P&L ${realized_pnl:.2f}")
        return {
            "success": True,
            "trade": {
                "symbol": symbol,
                "side": "SELL" if not is_short else "SHORT",
                "quantity": quantity,
                "price": price,
                "total_value": total_proceeds,
                "realized_pnl": round(realized_pnl, 2),
                "timestamp": datetime.utcnow().isoformat(),
            }
        }

    def auto_trade(self, signal: dict, current_price: float) -> dict:
        """Execute an auto-trade based on an AI signal with DCF weight ratio."""
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
            return {
                "success": False,
                "skipped": True,
                "reason": f"Confidence {confidence:.0%} below minimum {min_confidence:.0%}"
            }

        cash = self.get_cash_balance()
        total_equity = cash
        if not self.use_alpaca:
            for p in self.get_all_positions():
                total_equity += p.quantity * p.current_price
        else:
            try:
                # Use actual Alpaca equity for live sizing
                total_equity = float(self.alpaca.get_account().equity)
            except Exception:
                total_equity = cash
                
        # Use AI recommended optimal weight from DCF math if available (cap at 2x leverage), 
        # else fallback to default risk setting
        if weight is not None:
            target_allocation_pct = min(200.0, abs(float(weight)) * 100)
        else:
            target_allocation_pct = risk_per_trade_pct
            
        risk_amount = total_equity * (target_allocation_pct / 100)
        quantity = round(risk_amount / current_price, 4)

        if action in ["BUY", "COVER"]:
            if quantity < 0.001:
                return {"success": False, "error": f"Calculated BUY quantity too small for eq {total_equity}"}
            if action == "COVER":
                 pos = self.get_position(symbol)
                 if pos and pos.quantity < 0:
                      quantity = min(quantity, abs(pos.quantity))
                 else:
                      return {"success": False, "skipped": True, "reason": "No short position to cover"}
            return self.execute_buy(symbol, quantity, current_price, True, confidence, reasoning)

        elif action in ["SELL", "SHORT"]:
            if quantity < 0.001:
                 return {"success": False, "error": "Calculated SELL quantity too small"}
            if action == "SHORT":
                allow_short = get_setting(self.db, "allow_short_selling", self.user_id, "false") == "true"
                if not allow_short:
                    return {"success": False, "skipped": True, "reason": "Short selling disabled (enable in settings or upgrade to margin account)"}
            if action == "SELL":
                pos = self.get_position(symbol)
                if not pos or pos.quantity <= 0:
                    return {"success": False, "skipped": True, "reason": f"No long position in {symbol} to sell"}
                quantity = min(quantity, pos.quantity)
            return self.execute_sell(symbol, quantity, current_price, True, confidence, reasoning)

        return {"success": False, "skipped": True, "reason": "Signal is HOLD"}

    def update_position_prices(self, prices: dict):
        """Update current prices for all positions."""
        positions = self.get_all_positions()
        for pos in positions:
            if pos.symbol in prices:
                pos.current_price = prices[pos.symbol]
                
                # Reverse math for short position unrealized PNL
                if pos.quantity < 0:
                    pos.unrealized_pnl = (pos.avg_cost - pos.current_price) * abs(pos.quantity)
                else:
                    pos.unrealized_pnl = (pos.current_price - pos.avg_cost) * pos.quantity
                    
                pos.last_updated = datetime.utcnow()
        self.db.commit()

    def get_portfolio_summary(self) -> dict:
        """Calculate portfolio metrics (Alpaca or Paper)."""
        if self.use_alpaca:
            try:
                account = self.alpaca.get_account()
                positions_api = self.alpaca.list_positions()
                
                cash = float(account.cash)
                total_equity = float(account.equity)
                total_market_value = total_equity - cash
                initial_cash = float(get_setting(self.db, "initial_cash", self.user_id, "100000.0")) # Keep local for ref
                total_return = total_equity - initial_cash
                total_return_pct = (total_return / initial_cash * 100) if initial_cash > 0 else 0
                
                positions_data = []
                total_cost_basis = 0
                unrealized_pnl = 0
                
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
                        "symbol": p.symbol,
                        "quantity": round(qty, 4),
                        "avg_cost": round(avg_entry, 2),
                        "current_price": round(current_price, 2),
                        "market_value": round(market_val, 2),
                        "cost_basis": round(cost_basis, 2),
                        "unrealized_pnl": round(unrealized, 2),
                        "unrealized_pnl_pct": round(unrealized_pct, 2),
                        "weight_pct": round((abs(market_val) / total_equity * 100), 2) if total_equity > 0 else 0,
                    })
                
                all_trades = self.db.query(Trade).filter(Trade.user_id == self.user_id).all()
                return {
                    "cash": round(cash, 2),
                    "total_market_value": round(total_market_value, 2),
                    "total_equity": round(total_equity, 2),
                    "total_cost_basis": round(total_cost_basis, 2),
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "total_return": round(total_return, 2),
                    "total_return_pct": round(total_return_pct, 2),
                    "initial_cash": round(initial_cash, 2),
                    "total_trades": len(all_trades),
                    "positions": positions_data,
                    "provider": "Alpaca"
                }

            except Exception as e:
                logger.error(f"Alpaca get_portfolio_summary Error: {e}")
                # Fall open to local
        
        # Local DB Fallback / Paper mode
        positions = self.get_all_positions()
        cash = self.get_cash_balance()
        initial_cash = float(get_setting(self.db, "initial_cash", self.user_id, "100000.0"))

        total_market_value = sum(p.quantity * p.current_price for p in positions)
        total_cost_basis = sum(abs(p.quantity) * p.avg_cost for p in positions)
        
        # Recalculate PNL securely
        unrealized_pnl = 0
        total_equity = cash
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
                    "symbol": p.symbol,
                    "quantity": round(p.quantity, 4),
                    "avg_cost": round(p.avg_cost, 2),
                    "current_price": round(p.current_price, 2),
                    "market_value": round(p.quantity * p.current_price, 2),
                    "cost_basis": round(abs(p.quantity) * p.avg_cost, 2),
                    "unrealized_pnl": round(unrealized, 2),
                    "unrealized_pnl_pct": round(unrealized_pct, 2),
                    "weight_pct": round((abs(p.quantity) * p.current_price / total_equity * 100), 2) if total_equity > 0 else 0,
                })

        return {
            "cash": round(cash, 2),
            "total_market_value": round(total_market_value, 2),
            "total_equity": round(total_equity, 2),
            "total_cost_basis": round(total_cost_basis, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "total_return": round(total_return, 2),
            "total_return_pct": round(total_return_pct, 2),
            "initial_cash": round(initial_cash, 2),
            "total_trades": len(all_trades),
            "positions": positions_data,
            "provider": "Paper"
        }
