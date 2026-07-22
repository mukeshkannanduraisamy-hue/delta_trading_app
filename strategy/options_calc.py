"""Advanced Options Trading Calculations (Smart Entry, SL, TP, Greeks, IV)."""

import math
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from .delta_client import client
from . import config

# IV Proxy assumptions (since we don't have 52w historical IV easily)
DEFAULT_IV_LOW = 0.30
DEFAULT_IV_HIGH = 1.20

"""
adv_params Schema:
{
    # Fix 1 - ATR
    "atr_15m": float,
    "atr_1h": float,
    "atr_4h": float,
    "sl_atr": float,

    # Fix 2 - Theta
    "dte": int,
    "daily_theta": float,
    "theta_days_multiplier": float,
    "theta_buffer": float,
    "theta_warning": str | None,

    # Fix 3 - IV
    "current_iv": float,
    "ivr": float,
    "iv_buffer_pct": float,
    "iv_buffer": float,
    "iv_regime": str,
    "iv_warning": str | None,

    # Fix 4 - Gamma
    "gamma": float,
    "gamma_risk_factor": float,
    "gamma_warning": str | None,
    "option_premium_move": float,

    # Fix 5 - TP
    "tp1_ratio": float,
    "tp2_ratio": float,
    "tp3_ratio": float,
    "theta_cost_tp1": float,
    "theta_cost_tp2": float,
    "theta_cost_tp3": float,
    "tp1_achievable": bool,
    "tp2_achievable": bool,
    "tp3_achievable": bool,

    # Final Values
    "final_sl": float,
    "final_tp1": float | None,
    "final_tp2": float | None,
    "final_tp3": float | None,
    "sl_distance_pct": float,
    "risk_reward_tp1": float,
    "risk_reward_tp2": float,
    "risk_reward_tp3": float | None
}
"""

class AutoSkipTrade(Exception):
    pass

def calculate_atr(candles: list[dict], period: int = 14) -> float:
    """Calculate Average True Range (ATR)."""
    if len(candles) < period + 1:
        return 0.0
    
    true_ranges = []
    for i in range(1, len(candles)):
        high = float(candles[i].get("high", 0))
        low = float(candles[i].get("low", 0))
        prev_close = float(candles[i-1].get("close", 0))
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    
    # Simple moving average for ATR
    return sum(true_ranges[-period:]) / period


def get_smart_entry_price(orderbook: dict, side: str) -> float:
    if not orderbook:
        return 0.0
    
    bids = orderbook.get("buy", [])
    asks = orderbook.get("sell", [])
    
    if not bids or not asks:
        return 0.0
        
    best_bid = float(bids[0]["price"])
    best_ask = float(asks[0]["price"])
    spread_pct = ((best_ask - best_bid) / best_bid) * 100 if best_bid > 0 else 0
    
    if spread_pct <= 2.0:
        mid = (best_ask + best_bid) / 2.0
        return mid
    elif 2.0 < spread_pct <= 5.0:
        return (best_bid * 0.7) + (best_ask * 0.3)
    else:
        return best_bid * 1.01


def calculate_sl_multiplier(confidence: int) -> float:
    """Confidence Tier to ATR Multiplier."""
    if confidence <= 20: return 1.0
    elif confidence <= 40: return 1.5
    elif confidence <= 60: return 2.0
    elif confidence <= 80: return 2.5
    else: return 3.0


def calculate_options_order_params(symbol: str, direction: str, confidence: int, virtual_balance: float) -> dict:
    """
    Massive pre-trade calculation for BTC Options applying 5 critical fixes.
    """
    adv_params = {}
    warnings = []
    
    # ── INITIAL SETUP ──────────────────────────────────────────
    ticker = client.ticker(symbol)
    if not ticker or "greeks" not in ticker:
        raise ValueError(f"Failed to fetch ticker or greeks for {symbol}")
        
    spot = float(ticker["greeks"].get("spot", 0))
    delta = abs(float(ticker["greeks"].get("delta", 0.5)))
    
    # ── FIX 2: THETA BUFFER & DTE ──────────────────────────────
    try:
        expiry_str = symbol.split("-")[3]
        ex_date = datetime.strptime(expiry_str, "%d%m%y").replace(tzinfo=timezone.utc)
        dte = (ex_date - datetime.now(timezone.utc)).days
    except Exception:
        dte = 0
        
    adv_params["dte"] = dte
    
    if dte <= 1:
        raise AutoSkipTrade(f"TRADE BLOCKED: Option expires in {dte} days or less. Theta decay will destroy premium too fast.")
        
    daily_theta = abs(float(ticker["greeks"].get("theta", 0)))
    adv_params["daily_theta"] = daily_theta
    
    theta_warning = None
    if dte > 21:
        theta_days_multiplier = 1.0
    elif dte >= 8 and dte <= 21:
        theta_days_multiplier = 1.5
    elif dte >= 4 and dte <= 7:
        theta_days_multiplier = 2.0
        theta_warning = "Weekly option detected. Theta buffer increased to 2 days."
    else: # 2-3
        theta_days_multiplier = 3.0
        theta_warning = f"CAUTION: Near expiry option (DTE: {dte}). Theta is accelerating. Buffer set to 3 days."
        
    adv_params["theta_days_multiplier"] = theta_days_multiplier
    adv_params["theta_warning"] = theta_warning
    if theta_warning:
        warnings.append(theta_warning)
        
    theta_buffer = daily_theta * theta_days_multiplier
    adv_params["theta_buffer"] = theta_buffer
    
    # ── L2 SMART ENTRY ──────────────────────────────────────────
    ob = client.l2orderbook(symbol)
    smart_entry = get_smart_entry_price(ob, "buy")
    if smart_entry == 0:
        smart_entry = float(ticker.get("mark_price", 0))
        
    if smart_entry > 0 and (theta_buffer / smart_entry) * 100 > 25:
        warn = f"Theta buffer is {((theta_buffer/smart_entry)*100):.1f}% of premium. Time decay risk is very high."
        warnings.append(warn)
        if not adv_params["theta_warning"]:
            adv_params["theta_warning"] = warn
            
    # ── FIX 1: MULTI-TF ATR ─────────────────────────────────────
    now = int(time.time())
    c15 = client.candles(config.UNDERLYING_SYMBOL, "15m", now - 18000, now)  # 20 bars
    c1h = client.candles(config.UNDERLYING_SYMBOL, "1h", now - 72000, now)   # 20 bars
    c4h = client.candles(config.UNDERLYING_SYMBOL, "4h", now - 288000, now)  # 20 bars
    
    atr_15m = calculate_atr(c15, 14)
    atr_1h = calculate_atr(c1h, 14)
    atr_4h = calculate_atr(c4h, 14)
    
    if atr_15m == 0: atr_15m = spot * 0.005
    if atr_1h == 0: atr_1h = spot * 0.010
    
    sl_atr = max(atr_1h, atr_15m * 4)
    
    adv_params["atr_15m"] = atr_15m
    adv_params["atr_1h"] = atr_1h
    adv_params["atr_4h"] = atr_4h
    adv_params["sl_atr"] = sl_atr
    
    btc_sl_distance = sl_atr * calculate_sl_multiplier(confidence)
    
    if direction == "CE":
        btc_sl_price = spot - btc_sl_distance
    else:
        btc_sl_price = spot + btc_sl_distance

    # ── FIX 3: DYNAMIC IV BUFFER ───────────────────────────────
    current_iv = 0.0
    if "mark_iv" in ticker and ticker["mark_iv"] is not None:
        current_iv = float(ticker["mark_iv"])
    elif "quotes" in ticker and ticker["quotes"].get("mark_iv") is not None:
        current_iv = float(ticker["quotes"]["mark_iv"])
        
    ivr = ((current_iv - DEFAULT_IV_LOW) / (DEFAULT_IV_HIGH - DEFAULT_IV_LOW)) * 100
    
    iv_regime = ""
    iv_warning = None
    if ivr <= 20:
        iv_buffer_pct = 5.0
        iv_regime = "LOW IV"
    elif ivr <= 40:
        iv_buffer_pct = 8.0
        iv_regime = "BELOW AVERAGE IV"
    elif ivr <= 60:
        iv_buffer_pct = 12.0
        iv_regime = "AVERAGE IV"
    elif ivr <= 80:
        iv_buffer_pct = 20.0
        iv_regime = "HIGH IV"
        iv_warning = "High IV detected. Buffer increased to 20%."
    else:
        iv_buffer_pct = 30.0
        iv_regime = "VERY HIGH IV"
        iv_warning = f"WARNING: IVR {ivr:.1f}%. Extremely High IV risk."
        
    if getattr(config, "IV_SKIP_THRESHOLD", 85) and ivr > config.IV_SKIP_THRESHOLD:
        if direction == "CE" or direction == "PE": # User said "BUY" signal, meaning we are buying the option
            raise AutoSkipTrade(f"AUTO-SKIPPED: IVR {ivr:.1f}% exceeds {config.IV_SKIP_THRESHOLD}% threshold. Buying premium at extreme IV is high risk.")
        
    if iv_warning:
        warnings.append(iv_warning)
        
    iv_buffer = smart_entry * (iv_buffer_pct / 100)
    
    adv_params["current_iv"] = current_iv
    adv_params["ivr"] = ivr
    adv_params["iv_buffer_pct"] = iv_buffer_pct
    adv_params["iv_buffer"] = iv_buffer
    adv_params["iv_regime"] = iv_regime
    adv_params["iv_warning"] = iv_warning
    
    # ── FIX 4: GAMMA RISK ───────────────────────────────────────
    gamma = float(ticker["greeks"].get("gamma", 0.001))
    if gamma == 0: gamma = 0.001
    
    gamma_warning = None
    if dte > 14:
        gamma_risk_factor = 1.0
    elif dte >= 7 and dte <= 14:
        gamma_risk_factor = 1.2
    elif dte >= 4 and dte <= 6:
        gamma_risk_factor = 1.5
        gamma_warning = "Elevated Gamma risk. SL widened by 50%."
    else:
        gamma_risk_factor = 2.0
        gamma_warning = "HIGH Gamma risk detected. SL doubled."
        
    if gamma_warning:
        warnings.append(gamma_warning)
        
    option_premium_move = (delta * btc_sl_distance) + (0.5 * gamma * btc_sl_distance * btc_sl_distance)
    
    adv_params["gamma"] = gamma
    adv_params["gamma_risk_factor"] = gamma_risk_factor
    adv_params["gamma_warning"] = gamma_warning
    adv_params["option_premium_move"] = option_premium_move
    
    # Calculate SL
    raw_sl = smart_entry - option_premium_move - theta_buffer - iv_buffer
    sl_distance = smart_entry - raw_sl
    gamma_sl_distance = sl_distance * gamma_risk_factor
    
    final_option_sl = smart_entry - gamma_sl_distance
    
    # Enforce Hard Limits
    min_sl_distance = smart_entry * 0.10
    max_sl_distance = smart_entry * 0.90
    
    if (smart_entry - final_option_sl) < min_sl_distance:
        final_option_sl = smart_entry - min_sl_distance
    if (smart_entry - final_option_sl) > max_sl_distance:
        final_option_sl = smart_entry - max_sl_distance
        warnings.append("90% hard stop applied.")
        
    adv_params["final_sl"] = final_option_sl
    
    # ── FIX 5: DTE-SCALED TP RATIOS ─────────────────────────────
    if dte > 21:
        if confidence > 80: tp_ratios = (1.5, 2.5, 4.0)
        elif confidence >= 60: tp_ratios = (1.2, 2.0, 3.0)
        else: tp_ratios = (1.0, 1.5, 2.0)
    elif dte >= 8 and dte <= 21:
        if confidence > 80: tp_ratios = (1.5, 2.2, 3.0)
        elif confidence >= 60: tp_ratios = (1.2, 1.8, 2.5)
        else: tp_ratios = (1.0, 1.3, 1.8)
    elif dte >= 4 and dte <= 7:
        if confidence > 80: tp_ratios = (1.2, 1.8, 2.5)
        elif confidence >= 60: tp_ratios = (1.0, 1.5, 2.0)
        else: tp_ratios = (0.8, 1.2, 1.5)
    else:
        if confidence > 80: tp_ratios = (1.0, 1.5, 2.0)
        elif confidence >= 60: tp_ratios = (0.8, 1.2, 1.5)
        else: tp_ratios = (0.7, 1.0, 1.3)
        
    adv_params["tp1_ratio"], adv_params["tp2_ratio"], adv_params["tp3_ratio"] = tp_ratios
    
    risk = smart_entry - final_option_sl
    raw_tp1 = smart_entry + (risk * tp_ratios[0])
    raw_tp2 = smart_entry + (risk * tp_ratios[1])
    raw_tp3 = smart_entry + (risk * tp_ratios[2])
    
    btc_move_to_tp1 = (raw_tp1 - smart_entry) / delta if delta > 0 else 0
    days_to_tp1 = btc_move_to_tp1 / (sl_atr * 0.5) if sl_atr > 0 else 0.5
    days_to_tp1 = max(0.5, min(days_to_tp1, dte))
    
    btc_move_to_tp2 = (raw_tp2 - smart_entry) / delta if delta > 0 else 0
    days_to_tp2 = btc_move_to_tp2 / (sl_atr * 0.5) if sl_atr > 0 else 1.0
    days_to_tp2 = max(1.0, min(days_to_tp2, dte))
    
    btc_move_to_tp3 = (raw_tp3 - smart_entry) / delta if delta > 0 else 0
    days_to_tp3 = btc_move_to_tp3 / (sl_atr * 0.5) if sl_atr > 0 else 1.5
    days_to_tp3 = max(1.5, min(days_to_tp3, dte))
    
    theta_cost_tp1 = daily_theta * days_to_tp1
    theta_cost_tp2 = daily_theta * days_to_tp2
    theta_cost_tp3 = daily_theta * days_to_tp3
    
    final_tp1 = raw_tp1 - theta_cost_tp1
    final_tp2 = raw_tp2 - theta_cost_tp2
    final_tp3 = raw_tp3 - theta_cost_tp3
    
    tp1_achievable = final_tp1 > smart_entry
    tp2_achievable = final_tp2 > smart_entry
    tp3_achievable = final_tp3 > smart_entry
    
    if not tp1_achievable:
        warnings.append("WARNING: TP1 is not achievable after theta cost.")
    if not tp2_achievable:
        warnings.append("TP2 may not be achievable after theta cost.")
        final_tp2 = None
    if not tp3_achievable:
        warnings.append("TP3 removed: Not achievable after theta cost.")
        final_tp3 = None
        
    adv_params["theta_cost_tp1"] = theta_cost_tp1
    adv_params["theta_cost_tp2"] = theta_cost_tp2
    adv_params["theta_cost_tp3"] = theta_cost_tp3
    adv_params["tp1_achievable"] = tp1_achievable
    adv_params["tp2_achievable"] = tp2_achievable
    adv_params["tp3_achievable"] = tp3_achievable
    adv_params["final_tp1"] = final_tp1 if tp1_achievable else None
    adv_params["final_tp2"] = final_tp2
    adv_params["final_tp3"] = final_tp3
    
    adv_params["sl_distance_pct"] = ((smart_entry - final_option_sl) / smart_entry) * 100 if smart_entry > 0 else 0
    adv_params["risk_reward_tp1"] = (final_tp1 - smart_entry) / risk if risk > 0 and tp1_achievable else 0
    adv_params["risk_reward_tp2"] = (final_tp2 - smart_entry) / risk if risk > 0 and final_tp2 else 0
    adv_params["risk_reward_tp3"] = (final_tp3 - smart_entry) / risk if risk > 0 and final_tp3 else None

    # Dummy BTC TP levels for return dict compatibility if needed (not strictly required but kept for safety)
    btc_tp1 = spot + (btc_sl_distance * tp_ratios[0]) if direction == "CE" else spot - (btc_sl_distance * tp_ratios[0])
    btc_tp2 = spot + (btc_sl_distance * tp_ratios[1]) if direction == "CE" else spot - (btc_sl_distance * tp_ratios[1])
    btc_tp3 = spot + (btc_sl_distance * tp_ratios[2]) if direction == "CE" else spot - (btc_sl_distance * tp_ratios[2])

    return {
        "smart_entry": smart_entry,
        "btc_sl_price": btc_sl_price,
        "option_sl_price": final_option_sl,
        "option_tp1": final_tp1 if tp1_achievable else raw_tp1,
        "option_tp2": final_tp2 if final_tp2 else raw_tp2,
        "option_tp3": final_tp3 if final_tp3 else raw_tp3,
        "btc_tp1": btc_tp1,
        "btc_tp2": btc_tp2,
        "btc_tp3": btc_tp3,
        "entry_iv": current_iv,
        "spot": spot,
        "delta": delta,
        "theta": -daily_theta, # Keeping convention
        "atr": sl_atr,
        "ivr": ivr,
        "dte": dte,
        "warnings": warnings,
        "errors": [],
        "adv_params": adv_params
    }
