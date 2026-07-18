"""Central configuration for the strategy engine, loaded from the .env file."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# --------------------------------------------------------------------------- #
# API endpoints
# --------------------------------------------------------------------------- #
# Production public market data (deep, reliable) — used for signal candles.
PROD_BASE = "https://api.india.delta.exchange"
# Testnet demo base — the ONLY place the demo API keys are valid, and the only
# book we ever place real orders on.
TESTNET_BASE = "https://cdn-ind.testnet.deltaex.org"

API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
# "paper"     -> simulate fills against live mark price, no API orders.
# "live_demo" -> place real orders on the testnet demo book.
EXECUTION_MODE = os.getenv("EXECUTION_MODE", "paper").strip().lower()
LIVE_DEMO = EXECUTION_MODE == "live_demo"

# When live_demo, option resolution + orders must use the testnet book so the
# product actually exists there. In paper mode we resolve options from the
# richer production chain.
EXEC_BASE = TESTNET_BASE if LIVE_DEMO else PROD_BASE

ASSET = os.getenv("ENGINE_ASSET", "BTC").strip().upper()
# Underlying perpetual whose candles drive the directional strategies.
UNDERLYING_SYMBOL = f"{ASSET}USD"

POLL_SECONDS = int(os.getenv("ENGINE_POLL_SECONDS", "15"))
CONTRACTS = int(os.getenv("ENGINE_CONTRACTS", "1"))
AUTOSTART = os.getenv("ENGINE_AUTOSTART", "false").strip().lower() in {"1", "true", "yes"}

# Paper-trading virtual wallet (USD).
PAPER_START_BALANCE = float(os.getenv("PAPER_START_BALANCE", "10000"))

# Delta taker fee for options: 0.03% of notional, capped at 3.5% of premium,
# plus 18% GST. Applied to paper P&L so results are realistic (matches the
# fee model verified in the earlier autotrader backtests).
FEE_NOTIONAL_RATE = 0.0003
FEE_PREMIUM_CAP = 0.035
GST_RATE = 0.18

# Safety: never hold more than this many open positions across all strategies.
MAX_OPEN_POSITIONS = int(os.getenv("ENGINE_MAX_OPEN", "8"))

# Max bars a position may be held before a time-based exit (per strategy tf).
DEFAULT_MAX_HOLD_BARS = int(os.getenv("ENGINE_MAX_HOLD_BARS", "60"))

JOURNAL_DIR = BASE_DIR / "strategy" / "journal"
JOURNAL_DIR.mkdir(parents=True, exist_ok=True)


def summary() -> dict:
    """Non-secret snapshot of the active configuration for the UI / logs."""
    return {
        "execution_mode": EXECUTION_MODE,
        "live_demo": LIVE_DEMO,
        "asset": ASSET,
        "underlying": UNDERLYING_SYMBOL,
        "poll_seconds": POLL_SECONDS,
        "contracts": CONTRACTS,
        "autostart": AUTOSTART,
        "exec_base": EXEC_BASE,
        "paper_start_balance": PAPER_START_BALANCE,
        "has_keys": bool(API_KEY and API_SECRET),
    }
