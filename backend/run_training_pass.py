import sys
import logging
import json
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")
from database import get_db, set_setting, get_setting
import market_data as md
import deepseek_ai as ai
import asyncio
from trading_engine import TradingEngine

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

async def run_training_pass():
    print("🚀 Initiating Ollama (Local DeepSeek-R1) Multi-Factor Simulation Pass...")
    db = next(get_db())
    api_key = get_setting(db, "deepseek_api_key", "")
    ai_provider = get_setting(db, "ai_provider", "ollama")
    
    watchlist_json = get_setting(db, "watchlist", json.dumps(md.DEFAULT_WATCHLIST))
    watchlist = json.loads(watchlist_json)
    
    engine = TradingEngine(db)
    
    for symbol in watchlist:
        print(f"\n[SCAN] {symbol}...")
        try:
            quote = md.get_stock_quote(symbol)
            if not quote: 
                print(f"  -> Skipped (No Quote)")
                continue
                
            history = md.get_stock_history(symbol, period="6mo")
            indicators = md.get_technical_indicators(symbol)
            news = md.get_stock_news(symbol)
            
            signal = ai.analyze_stock(ai_provider, api_key, symbol, quote, indicators, history, news)
            
            action = signal.get("signal", "HOLD")
            conf = signal.get("confidence", 0)
            weight = signal.get("recommended_weight_pct", "N/A")
            
            print(f"  -> DCF Value: ${quote.get('dcf_value')} | Gap: {quote.get('valuation_gap_pct', 0)*100:.1f}%")
            print(f"  -> VPA: {quote.get('vpa_signal')} | Liq: {quote.get('liquidity')}")
            print(f"  -> AI Decision: {action} (Conf: {conf}) | Lev: {weight}")
            print(f"  -> Reason: {signal.get('reasoning')[:150]}...")
            
            if action in ["BUY", "SELL", "SHORT", "COVER"]:
                res = engine.auto_trade(signal, quote["current"])
                if res.get("success"):
                    print(f"  ✅ EXECUTED: {action} {symbol} - {res}")
                else:
                    print(f"  ❌ SUBMIT FAILED/SKIPPED: {res}")
                    
            await asyncio.sleep(2) # Prevent API rate limits
        except Exception as e:
            print(f"  💥 Error processing {symbol}: {e}")
            
    summary = engine.get_portfolio_summary()
    print("\n🏆 Sim Pass Complete! Current Stats:")
    print(f"  Total Equity: ${summary['total_equity']}")
    print(f"  Positions: {len(summary['positions'])}")

if __name__ == "__main__":
    asyncio.run(run_training_pass())
