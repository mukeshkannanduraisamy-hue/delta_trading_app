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
    """
    Calculate Smart Entry Price using L2 Orderbook.
    side: 'buy' or 'sell' (usually we are 'buy'ing options to enter long, or 'sell'ing to close)
    """
    if not orderbook:
        return 0.0
    
    bids = orderbook.get("buy", [])
    asks = orderbook.get("sell", [])
    
    if not bids or not asks:
        return 0.0
        
    best_bid = float(bids[0]["price"])
    best_ask = float(asks[0]["price"])
    spread_pct = ((best_ask - best_bid) / best_bid) * 100 if best_bid > 0 else 0
    
    # 3. Spread-Based Entry Logic
    if spread_pct <= 2.0:
        # TIGHT SPREAD: Place limit order at mid-price + 1 tick
        # Wait, if we buy, mid-price + 1 tick closer to ask
        mid = (best_ask + best_bid) / 2.0
        return mid # + some small tick offset ideally
    elif 2.0 < spread_pct <= 5.0:
        # WIDE SPREAD: Place limit order at (Best Bid * 0.7) + (Best Ask * 0.3)
        return (best_bid * 0.7) + (best_ask * 0.3)
    else:
        # VERY WIDE SPREAD: Place limit order strictly at Best Bid + 1 tick
        return best_bid * 1.01 # +1% as a proxy for 1 tick, or just best bid


def calculate_sl_multiplier(confidence: int) -> float:
    """Confidence Tier to ATR Multiplier."""
    if confidence <= 20: return 1.0
    elif confidence <= 40: return 1.5
    elif confidence <= 60: return 2.0
    elif confidence <= 80: return 2.5
    else: return 3.0


def calculate_options_order_params(symbol: str, direction: str, confidence: int, virtual_balance: float) -> dict:
    """
    Massive pre-trade calculation for BTC Options.
    direction: "CE" or "PE"
    """
    # 1. Fetch Option Ticker (contains Greeks, IV, Spot)
    ticker = client.ticker(symbol)
    if not ticker or "greeks" not in ticker:
        raise ValueError(f"Failed to fetch ticker or greeks for {symbol}")
        
    spot = float(ticker["greeks"].get("spot", 0))
    delta = abs(float(ticker["greeks"].get("delta", 0.5)))
    theta = float(ticker["greeks"].get("theta", 0))
    
    entry_iv = 0.0
    if "mark_iv" in ticker and ticker["mark_iv"] is not None:
        entry_iv = float(ticker["mark_iv"])
    elif "quotes" in ticker and ticker["quotes"].get("mark_iv") is not None:
        entry_iv = float(ticker["quotes"]["mark_iv"])
        
    # 2. Fetch L2 Orderbook for Smart Entry
    ob = client.l2orderbook(symbol)
    smart_entry = get_smart_entry_price(ob, "buy")
    if smart_entry == 0:
        smart_entry = float(ticker.get("mark_price", 0))

    # 3. Fetch BTC Candles for ATR (15m timeframe)
    now = int(time.time())
    # 15m * 20 bars = 300 minutes = 18000 seconds
    candles = client.candles(config.UNDERLYING_SYMBOL, "15m", now - 18000, now)
    atr = calculate_atr(candles, 14)
    if atr == 0:
        atr = spot * 0.005 # fallback 0.5% ATR

    # 4. BTC Stop Loss Price (Underlying)
    multiplier = calculate_sl_multiplier(confidence)
    atr_distance = atr * multiplier
    
    if direction == "CE":
        btc_sl_price = spot - atr_distance
    else:
        btc_sl_price = spot + atr_distance

    # 5. Option Stop Loss Price (Premium)
    # SL = Entry Premium - (Delta * ATR Distance) - (Theta * 1 day) - IV Buffer (10%)
    iv_buffer = smart_entry * 0.10
    option_sl_price = smart_entry - (delta * atr_distance) - abs(theta) - iv_buffer
    option_sl_price = max(option_sl_price, smart_entry * 0.10) # Max 90% loss

    # 6. Option Take Profit Levels (40/40/20)
    # Using dynamic RR based on Confidence
    if confidence >= 80:
        rr1, rr2, rr3 = 1.5, 2.5, 4.0
    elif confidence >= 60:
        rr1, rr2, rr3 = 1.2, 2.0, 3.0
    else:
        rr1, rr2, rr3 = 1.0, 1.5, 2.0

    premium_risk = smart_entry - option_sl_price
    option_tp1 = smart_entry + (premium_risk * rr1)
    option_tp2 = smart_entry + (premium_risk * rr2)
    option_tp3 = smart_entry + (premium_risk * rr3)

    # 7. BTC Take Profit Levels
    btc_risk = atr_distance
    if direction == "CE":
        btc_tp1 = spot + (btc_risk * rr1)
        btc_tp2 = spot + (btc_risk * rr2)
        btc_tp3 = spot + (btc_risk * rr3)
    else:
        btc_tp1 = spot - (btc_risk * rr1)
        btc_tp2 = spot - (btc_risk * rr2)
        btc_tp3 = spot - (btc_risk * rr3)

    # 8. Risk Validations
    warnings = []
    errors = []
    
    # IV Rank (Proxy)
    ivr = ((entry_iv - DEFAULT_IV_LOW) / (DEFAULT_IV_HIGH - DEFAULT_IV_LOW)) * 100
    if ivr > 80:
        warnings.append(f"High IV Rank ({ivr:.1f}%). Option is expensive.")
    
    # Days to Expiry (DTE)
    try:
        # C-BTC-66000-240726 -> 240726
        expiry_str = symbol.split("-")[3]
        ex_date = datetime.strptime(expiry_str, "%d%m%y").replace(tzinfo=timezone.utc)
        dte = (ex_date - datetime.now(timezone.utc)).days
        if dte < 3:
            warnings.append(f"Short DTE ({dte} days). Gamma risk is high.")
    except Exception:
        dte = 0

    # Max Option Risk (2% of balance)
    max_risk = virtual_balance * 0.02
    # Assume 1 contract for now to check min premium risk
    # Contract value usually 0.001
    contract_val = float(ticker.get("contract_value", 0.001))
    risk_per_contract = premium_risk * contract_val
    if risk_per_contract > max_risk:
        warnings.append(f"Risk per contract (${risk_per_contract:.2f}) exceeds 2% max risk (${max_risk:.2f}).")
        
    if smart_entry < 5.0: # Arbitrary min premium
        errors.append(f"Premium too low (${smart_entry:.2f}). Minimum is $5.0.")

    return {
        "smart_entry": smart_entry,
        "btc_sl_price": btc_sl_price,
        "option_sl_price": option_sl_price,
        "option_tp1": option_tp1,
        "option_tp2": option_tp2,
        "option_tp3": option_tp3,
        "btc_tp1": btc_tp1,
        "btc_tp2": btc_tp2,
        "btc_tp3": btc_tp3,
        "entry_iv": entry_iv,
        "spot": spot,
        "delta": delta,
        "theta": theta,
        "atr": atr,
        "ivr": ivr,
        "dte": dte,
        "warnings": warnings,
        "errors": errors
    }
