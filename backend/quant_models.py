import math
import logging

logger = logging.getLogger(__name__)

class QuantitativeModels:
    @staticmethod
    def calculate_dcf(fcf: float, total_debt: float, cash: float, shares_out: float, 
                      wacc: float = 0.10, growth_rate: float = 0.05, terminal_growth: float = 0.025, years: int = 5) -> float:
        """
        Simplified Discounted Cash Flow (DCF) model to estimate intrinsic equity value per share.
        :param fcf: Trailing twelve months Free Cash Flow.
        :param total_debt: Total Debt (short + long term).
        :param cash: Total Cash and Short Term Investments.
        :param shares_out: Total Shares Outstanding.
        :return: Intrinsic value per share, or 0 if inputs are invalid.
        """
        if not fcf or shares_out <= 0 or fcf <= 0:
            return 0.0
            
        pv_fcf = 0.0
        projected_fcf = fcf
        
        # Project 5 years
        for i in range(1, years + 1):
            projected_fcf *= (1 + growth_rate)
            pv_fcf += projected_fcf / ((1 + wacc) ** i)
            
        # Terminal Value
        terminal_value = (projected_fcf * (1 + terminal_growth)) / (wacc - terminal_growth)
        pv_terminal_value = terminal_value / ((1 + wacc) ** years)
        
        enterprise_value = pv_fcf + pv_terminal_value
        equity_value = enterprise_value + cash - total_debt
        
        value_per_share = equity_value / shares_out
        return max(0.0, value_per_share)

    @staticmethod
    def calculate_ddm(annual_dividend: float, wacc: float = 0.08, growth_rate: float = 0.03) -> float:
        """
        Gordon Growth Model (DDM) for dividend paying stocks.
        """
        if not annual_dividend or annual_dividend <= 0 or wacc <= growth_rate:
            return 0.0
        next_div = annual_dividend * (1 + growth_rate)
        return next_div / (wacc - growth_rate)
        
    @staticmethod
    def calculate_valuation_gap(current_price: float, intrinsic_value: float) -> float:
        """
        Returns the valuation gap percentage. 
        Positive = Overvalued (Premium). Negative = Undervalued (Discount).
        """
        if intrinsic_value <= 0 or current_price <= 0:
            return 0.0
        return (current_price - intrinsic_value) / current_price

    @staticmethod
    def analyze_volume_price_action(hist_data: list) -> dict:
        """
        Analyze recent OHLCV history to detect institutional footprints.
        Returns crowding metric and accumulation/distribution signals.
        """
        if not hist_data or len(hist_data) < 5:
            return {"vpa_signal": "Neutral", "crowding": 0.0, "liquidity": "Unknown"}
            
        recent = hist_data[-5:]
        avg_vol = sum(d["volume"] for d in hist_data[-20:]) / max(1, min(20, len(hist_data)))
        
        latest = recent[-1]
        candle_size = latest["high"] - latest["low"]
        body_size = abs(latest["close"] - latest["open"])
        vol_ratio = latest["volume"] / avg_vol if avg_vol > 0 else 1.0
        
        signal = "Neutral"
        # High volume, long upper wick -> Distribution
        if vol_ratio > 1.5 and (latest["high"] - max(latest["open"], latest["close"])) > body_size * 2:
            signal = "Strong Distribution (Bearish)"
        # High volume, long lower wick -> Accumulation
        elif vol_ratio > 1.5 and (min(latest["open"], latest["close"]) - latest["low"]) > body_size * 2:
            signal = "Strong Accumulation (Bullish)"
            
        crowding = min(1.0, vol_ratio / 3.0) # Simple proxy for crowding/retail FOMO
        liquidity = "High" if avg_vol > 1000000 else "Medium" if avg_vol > 100000 else "Low"
        
        return {
            "vpa_signal": signal,
            "crowding": round(crowding, 2),
            "liquidity": liquidity,
            "volume_ratio": round(vol_ratio, 2)
        }

    @staticmethod
    def calculate_optimal_allocation(val_gap: float, confidence: float, max_leverage: float = 2.0) -> float:
        """
        Calculate suggested portfolio allocation ratio (like Kelly sizing).
        Specifically tuned for a small account needing bold, short-biased sizing.
        :param val_gap: Float, e.g., 0.50 means 50% overvalued.
        :param confidence: AI signal confidence (0.0 to 1.0).
        :return: Allocation ratio (e.g., -0.75 means short 75% of equity).
        """
        # Base allocation factor depending on absolute valuation gap
        # Since the user specifically requested bold sizing and taking advantage of shorting:
        # A positive val_gap (overvalued) leads to a negative weight (SHORT).
        base_weight = val_gap * confidence * -1.0
        
        # We amplify the weight significantly for short selling opportunities
        # If val_gap > 0 (Overvalued, Short Signal), we apply a 2.5x aggression multiplier.
        # If val_gap < 0 (Undervalued, Long Signal), we still use a solid 1.5x multiplier.
        aggression_multiplier = 2.5 if base_weight < 0 else 1.5
        
        raw_weight = base_weight * aggression_multiplier
        
        # Apply a dampening function to prevent blowing out accounts, but max out at 2.0x leverage (margin limits)
        damped_weight = max(-max_leverage, min(max_leverage, raw_weight))
        return round(damped_weight, 3)
