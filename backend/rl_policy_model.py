"""
RL Auxiliary Policy Model (Path 1)
XGBoost regressor trained on historical RL JSONL data to predict expected
3-day reward for a given signal + market state.

Acts as a second-opinion filter on top of the LLM:
  - Negative RL score + marginal LLM confidence → skip trade
  - Strong positive RL score → slight confidence boost in log
  - Score surfaced in trade metadata for attribution

Retrained daily via main.py scheduled task.
"""
import math
import os
import logging
import pickle

import numpy as np

logger = logging.getLogger(__name__)

MODEL_FILE = os.path.join(os.path.dirname(__file__), "..", "rl_policy_model.pkl")
RL_DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "rl_training_data.jsonl")

# Minimum training samples required before we trust the model
_MIN_SAMPLES = 200

SECTOR_MAP = {
    "Technology": 0, "Semi": 1, "EV": 2, "AI": 3, "Cloud": 4,
    "Finance": 5, "Energy": 6, "Healthcare": 7, "Consumer": 8,
    "Defense": 9, "Crypto": 10, "ETF": 11, "China": 12,
    "Silver": 13, "Materials": 14, "Other": 15,
}

VPA_MAP = {
    "strong_buying": 2, "buying": 1, "neutral": 0,
    "selling": -1, "strong_selling": -2,
}


def _extract_features(rec: dict) -> list | None:
    """
    Extract a fixed-length numeric feature vector from a JSONL record.
    Returns None if the record is unusable (e.g. zero entry price).
    """
    try:
        state = rec.get("state", {}) or {}
        indicators = state.get("indicators", {}) or {}

        price = state.get("price") or 0
        if price <= 0:
            return None

        low52  = state.get("fifty_two_week_low")  or price
        high52 = state.get("fifty_two_week_high") or price
        week52_pos = (price - low52) / (high52 - low52) if high52 > low52 else 0.5

        action = rec.get("action", "HOLD")
        action_enc = 1 if action == "BUY" else (-1 if action in ("SELL", "SHORT") else 0)

        vpa_raw = state.get("vpa_signal") or indicators.get("vpa_signal") or ""
        rsi  = indicators.get("rsi")  or indicators.get("RSI")  or 50
        macd = indicators.get("macd") or indicators.get("MACD") or 0
        atr  = indicators.get("atr")  or indicators.get("ATR")  or 0

        features = [
            float(rec.get("confidence", 0)),          # 0: LLM confidence
            float(action_enc),                         # 1: action direction
            float(state.get("change_pct") or 0),       # 2: day change %
            math.log1p(abs(float(state.get("volume") or 0))),  # 3: log volume
            float(state.get("pe_ratio") or 0),         # 4: PE ratio
            float(state.get("vpa_volume_ratio") or 1), # 5: VPA volume ratio
            float(VPA_MAP.get(str(vpa_raw).lower(), 0)),  # 6: VPA signal encoded
            float(state.get("valuation_gap_pct") or 0),  # 7: DCF valuation gap
            float(week52_pos),                         # 8: position in 52w range
            float(SECTOR_MAP.get(rec.get("sector", "Other"), 15)),  # 9: sector
            float(rsi),                                # 10: RSI
            float(macd),                               # 11: MACD
            float(atr),                                # 12: ATR
        ]

        # Sanitize NaN/inf → 0
        return [0.0 if not math.isfinite(x) else x for x in features]

    except Exception:
        return None


def train_model() -> dict:
    """
    Train XGBoost regressor on all JSONL records that have reward_3d filled.
    Saves the model to MODEL_FILE.  Called daily by the scheduler.
    """
    try:
        from xgboost import XGBRegressor
    except ImportError:
        logger.error("[RL Policy] xgboost not installed")
        return {"error": "xgboost not installed"}

    if not os.path.exists(RL_DATA_FILE):
        return {"error": "RL data file not found"}

    import rl_data_collector as _rl
    records = _rl._parse_jsonl(RL_DATA_FILE)

    X, y = [], []
    for rec in records:
        reward = rec.get("reward_3d")
        if reward is None or not isinstance(reward, (int, float)) or not math.isfinite(reward):
            continue
        feats = _extract_features(rec)
        if feats is None:
            continue
        X.append(feats)
        y.append(float(reward))

    if len(X) < _MIN_SAMPLES:
        msg = f"Only {len(X)} labeled samples (need {_MIN_SAMPLES}), skipping training"
        logger.warning(f"[RL Policy] {msg}")
        return {"error": msg, "samples": len(X)}

    X_np = np.array(X, dtype=np.float32)
    y_np = np.array(y, dtype=np.float32)

    model = XGBRegressor(
        n_estimators=300,
        max_depth=6,
        learning_rate=0.04,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=5,
        gamma=0.1,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_np, y_np)

    with open(MODEL_FILE, "wb") as f:
        pickle.dump(model, f)

    logger.info(f"[RL Policy] Trained on {len(X)} samples → {MODEL_FILE}")
    return {"samples": len(X), "model_file": MODEL_FILE}


# --- Inference (hot-reloads model if file changes on disk) ---

_model_cache = None
_model_mtime  = 0.0


def _load_model():
    global _model_cache, _model_mtime
    if not os.path.exists(MODEL_FILE):
        return None
    try:
        mtime = os.path.getmtime(MODEL_FILE)
        if _model_cache is None or mtime > _model_mtime:
            with open(MODEL_FILE, "rb") as f:
                _model_cache = pickle.load(f)
            _model_mtime = mtime
    except Exception as e:
        logger.debug(f"[RL Policy] Model load error: {e}")
        return None
    return _model_cache


def predict_reward(signal: dict, quote: dict, indicators: dict) -> float | None:
    """
    Predict expected 3-day reward % for a candidate trade.
    Returns None if model is not yet trained or features are invalid.
    """
    model = _load_model()
    if model is None:
        return None

    rec = {
        "action":     signal.get("signal", "HOLD"),
        "confidence": signal.get("confidence", 0),
        "sector":     signal.get("sector", "Other"),
        "state": {
            "price":               quote.get("current"),
            "change_pct":          quote.get("change_pct"),
            "volume":              quote.get("volume"),
            "pe_ratio":            quote.get("pe_ratio"),
            "fifty_two_week_low":  quote.get("fifty_two_week_low"),
            "fifty_two_week_high": quote.get("fifty_two_week_high"),
            "vpa_signal":          quote.get("vpa_signal"),
            "vpa_volume_ratio":    quote.get("vpa_volume_ratio"),
            "valuation_gap_pct":   quote.get("valuation_gap_pct"),
            "indicators":          indicators or {},
        },
    }

    feats = _extract_features(rec)
    if feats is None:
        return None

    try:
        pred = float(model.predict(np.array([feats], dtype=np.float32))[0])
        return round(pred, 4) if math.isfinite(pred) else None
    except Exception as e:
        logger.debug(f"[RL Policy] Predict error: {e}")
        return None


def get_model_stats() -> dict:
    """Return basic stats about the trained model."""
    if not os.path.exists(MODEL_FILE):
        return {"trained": False}
    mtime = os.path.getmtime(MODEL_FILE)
    from datetime import datetime
    return {
        "trained": True,
        "model_file": MODEL_FILE,
        "last_trained": datetime.fromtimestamp(mtime).isoformat(),
    }
