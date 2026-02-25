"""
Kronos K-Line Analysis
======================
Integrates the Kronos foundation model (NeoQuasar/Kronos-base) for candlestick
forecasting on A100 GPU. Kronos was trained on 45+ global exchanges, making it
the first open-source foundation model specifically for K-line (OHLCV) data.

Paper: https://arxiv.org/abs/2508.02739
GitHub: https://github.com/shiyu-coder/Kronos

Usage in trading pipeline:
  Kronos predicts next 5 candles (OHLCV) → we extract predicted direction,
  expected return %, predicted high/low range → fed as context to DeepSeek AI.

Model loaded once at startup and kept in GPU memory (A100 80GB).
"""
import sys
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")
sys.path.insert(0, "/data/home/qbao775/stock-trading-platform/kronos_lib")

import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ── Lazy model loading (loaded once, cached globally) ─────────────────────────
_kronos_model = None
_kronos_tokenizer = None
_kronos_predictor = None
_device = "cuda"  # A100 GPU

KRONOS_TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-base"
KRONOS_MODEL_ID = "NeoQuasar/Kronos-base"   # Use base model on A100 80GB

LOOKBACK_BARS = 400    # Number of historical candles fed to Kronos
PRED_BARS = 5          # Predict next 5 trading sessions (~1 week)


def _load_model():
    """Load Kronos model and tokenizer (runs once, cached globally)."""
    global _kronos_model, _kronos_tokenizer, _kronos_predictor
    if _kronos_predictor is not None:
        return _kronos_predictor

    try:
        import torch
        from model import Kronos, KronosTokenizer, KronosPredictor

        logger.info(f"[Kronos] Loading tokenizer: {KRONOS_TOKENIZER_ID}")
        _kronos_tokenizer = KronosTokenizer.from_pretrained(KRONOS_TOKENIZER_ID)

        logger.info(f"[Kronos] Loading model: {KRONOS_MODEL_ID} on {_device}")
        _kronos_model = Kronos.from_pretrained(KRONOS_MODEL_ID)
        if torch.cuda.is_available():
            _kronos_model = _kronos_model.to(_device)
        _kronos_model.eval()

        _kronos_predictor = KronosPredictor(
            _kronos_model, _kronos_tokenizer, max_context=512
        )
        logger.info(f"[Kronos] Model loaded successfully on {'GPU (A100)' if torch.cuda.is_available() else 'CPU'}")
        return _kronos_predictor

    except Exception as e:
        logger.error(f"[Kronos] Failed to load model: {e}")
        return None


def _yfinance_to_kronos_df(history_df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """
    Convert yfinance history DataFrame to Kronos-compatible OHLCV format.
    yfinance columns: Open, High, Low, Close, Volume (capitalized)
    Kronos wants: open, high, low, close, volume (lowercase)
    """
    if history_df is None:
        return None
    # get_stock_history() may return a list on error
    if not isinstance(history_df, pd.DataFrame) or history_df.empty:
        return None

    df = history_df.copy()
    df.columns = [c.lower() for c in df.columns]

    # Ensure required columns exist
    required = ["open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        logger.debug(f"[Kronos] Missing columns: {missing}")
        return None

    df = df[required].dropna()

    # Take last LOOKBACK_BARS candles
    if len(df) > LOOKBACK_BARS:
        df = df.iloc[-LOOKBACK_BARS:]

    # Kronos needs at least 50 candles
    if len(df) < 50:
        logger.debug(f"[Kronos] Not enough history: {len(df)} bars")
        return None

    # Add 'amount' column (turnover) — Kronos supports it but it's optional
    # We use volume * close as a proxy if not available
    if "amount" not in df.columns:
        df["amount"] = df["volume"] * df["close"]

    return df


def predict_next_candles(symbol: str, history_df: pd.DataFrame) -> Optional[dict]:
    """
    Run Kronos forecast for the next PRED_BARS candles.

    Args:
        symbol: Stock ticker symbol
        history_df: yfinance history DataFrame (daily candles recommended)

    Returns:
        dict with predicted OHLCV + derived signals, or None on failure
    """
    predictor = _load_model()
    if predictor is None:
        return None

    df = _yfinance_to_kronos_df(history_df)
    if df is None:
        return None

    try:
        current_price = float(df["close"].iloc[-1])
        last_ts = df.index[-1]
        if hasattr(last_ts, "to_pydatetime"):
            last_ts = last_ts.to_pydatetime()

        # Generate future timestamps (business days)
        future_timestamps = pd.bdate_range(
            start=last_ts + timedelta(days=1),
            periods=PRED_BARS
        )

        x_timestamp = pd.Series(df.index)
        y_timestamp = pd.Series(future_timestamps)

        logger.info(f"[Kronos] Predicting {PRED_BARS} candles for {symbol} ({len(df)} bars input)")

        pred_df = predictor.predict(
            df=df[["open", "high", "low", "close", "volume", "amount"]],
            x_timestamp=x_timestamp,
            y_timestamp=y_timestamp,
            pred_len=PRED_BARS,
            T=1.0,       # Temperature: 1.0 = balanced diversity
            top_p=0.9,   # Nucleus sampling
            sample_count=5,  # Average 5 samples for stability
            verbose=False
        )

        if pred_df is None or pred_df.empty:
            return None

        # ── Derive Trading Signals ────────────────────────────────────────────
        pred_close_prices = pred_df["close"].values.tolist()
        pred_high_prices = pred_df["high"].values.tolist()
        pred_low_prices = pred_df["low"].values.tolist()

        final_pred_close = pred_close_prices[-1]
        expected_return_pct = (final_pred_close - current_price) / current_price * 100
        predicted_high = max(pred_high_prices)
        predicted_low = min(pred_low_prices)
        price_range_pct = (predicted_high - predicted_low) / current_price * 100

        # Direction signal
        if expected_return_pct > 1.5:
            kronos_signal = "BULLISH"
        elif expected_return_pct < -1.5:
            kronos_signal = "BEARISH"
        else:
            kronos_signal = "NEUTRAL"

        # Trend consistency: how many of the 5 days trend in the predicted direction?
        if expected_return_pct > 0:
            consistent_days = sum(1 for p in pred_close_prices if p > current_price)
        else:
            consistent_days = sum(1 for p in pred_close_prices if p < current_price)
        confidence = consistent_days / PRED_BARS  # 0.0 - 1.0

        result = {
            "symbol": symbol,
            "current_price": round(current_price, 4),
            "pred_close_day1": round(pred_close_prices[0], 4) if pred_close_prices else None,
            "pred_close_final": round(final_pred_close, 4),
            "expected_return_pct": round(expected_return_pct, 2),
            "predicted_high": round(predicted_high, 4),
            "predicted_low": round(predicted_low, 4),
            "price_range_pct": round(price_range_pct, 2),
            "kronos_signal": kronos_signal,
            "trend_consistency": round(confidence, 2),
            "pred_bars": PRED_BARS,
            "input_bars": len(df),
            "model": KRONOS_MODEL_ID,
        }

        logger.info(
            f"[Kronos] {symbol}: {kronos_signal} | "
            f"Current: ${current_price:.2f} → Predicted (day5): ${final_pred_close:.2f} "
            f"({expected_return_pct:+.2f}%) | Range: ${predicted_low:.2f}-${predicted_high:.2f} | "
            f"Consistency: {confidence:.0%}"
        )
        return result

    except Exception as e:
        logger.error(f"[Kronos] Prediction error for {symbol}: {e}")
        return None


def build_kronos_context(pred: dict) -> str:
    """
    Format Kronos prediction as AI prompt context.
    The DeepSeek/Ollama model reads this alongside technical indicators.
    """
    if not pred:
        return ""

    signal = pred["kronos_signal"]
    ret = pred["expected_return_pct"]
    consistency = pred["trend_consistency"]
    symbol = pred["symbol"]

    signal_emoji = {"BULLISH": "📈", "BEARISH": "📉", "NEUTRAL": "➡️"}.get(signal, "")

    lines = [
        f"### {signal_emoji} Kronos K-Line Foundation Model Forecast for {symbol}",
        f"(Model: {pred['model']} | Input: {pred['input_bars']} daily candles | Predicts next {pred['pred_bars']} sessions)",
        f"",
        f"  Current Price:       ${pred['current_price']:,.4f}",
        f"  Predicted Day 1:     ${pred['pred_close_day1']:,.4f}",
        f"  Predicted Day {pred['pred_bars']} Close: ${pred['pred_close_final']:,.4f}",
        f"  Expected Return:     {ret:+.2f}%",
        f"  Predicted Range:     ${pred['predicted_low']:,.4f} – ${pred['predicted_high']:,.4f} ({pred['price_range_pct']:.1f}% range)",
        f"  Trend Consistency:   {consistency:.0%} of predicted days trend {signal.lower()}",
        f"  Kronos Signal:       **{signal}**",
    ]

    # Instruction for AI based on Kronos signal + confidence
    if signal == "BULLISH" and consistency >= 0.6:
        lines.append(
            f"\n  ✅ INSTRUCTION: Kronos model has HIGH CONFIDENCE ({consistency:.0%}) that "
            f"{symbol} will rise {ret:+.2f}% over the next {pred['pred_bars']} sessions. "
            f"This is a quantitative K-line signal — incorporate into BUY decision."
        )
    elif signal == "BEARISH" and consistency >= 0.6:
        lines.append(
            f"\n  ❌ INSTRUCTION: Kronos model has HIGH CONFIDENCE ({consistency:.0%}) that "
            f"{symbol} will fall {ret:+.2f}% over the next {pred['pred_bars']} sessions. "
            f"This is a quantitative K-line signal — incorporate into SELL/AVOID decision."
        )
    elif signal == "BULLISH" and consistency >= 0.4:
        lines.append(
            f"\n  📌 INFO: Kronos model shows MODERATE bullish signal ({ret:+.2f}%, {consistency:.0%} consistent). "
            f"Use alongside other signals — not sufficient alone for trade."
        )
    elif signal == "BEARISH" and consistency >= 0.4:
        lines.append(
            f"\n  📌 INFO: Kronos model shows MODERATE bearish signal ({ret:+.2f}%, {consistency:.0%} consistent). "
            f"Use alongside other signals."
        )
    else:
        lines.append(
            f"\n  ℹ️  INFO: Kronos model is NEUTRAL or LOW CONFIDENCE on {symbol}. "
            f"Do not rely on this signal for trade direction."
        )

    return "\n".join(lines)


def preload_model():
    """Call this at startup to pre-warm the model on GPU."""
    logger.info("[Kronos] Pre-loading model on A100 GPU...")
    _load_model()
