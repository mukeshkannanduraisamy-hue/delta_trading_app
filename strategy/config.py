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
# Production public market data (deep, reliable)
PROD_BASE = "https://api.india.delta.exchange"
# Testnet demo base is removed for paper trading against prod data
TESTNET_BASE = PROD_BASE

API_KEY = ""
API_SECRET = ""

# --------------------------------------------------------------------------- #
# Execution — LIVE ONLY
# --------------------------------------------------------------------------- #
# Paper execution mode (simulated internally using production data)
EXECUTION_MODE = "paper"
LIVE_DEMO = False

EXEC_BASE = PROD_BASE

_requested_mode = os.getenv("EXECUTION_MODE", "").strip().lower()

ASSET = os.getenv("ENGINE_ASSET", "BTC").strip().upper()
# Underlying perpetual whose candles drive the directional strategies.
UNDERLYING_SYMBOL = f"{ASSET}USD"

# Prices now come from the in-memory WebSocket bus and strategy evaluation is
# gated on the bar clock, so a fast poll costs almost nothing and makes stop-loss
# reaction near-instant. Floor of 0.25s guards against a busy-spin.
POLL_SECONDS = max(0.25, float(os.getenv("ENGINE_POLL_SECONDS", "1")))
CONTRACTS = max(1, int(os.getenv("ENGINE_CONTRACTS", "1")))
AUTOSTART = os.getenv("ENGINE_AUTOSTART", "false").strip().lower() in {"1", "true", "yes"}

# Entry order type (live_demo only). "market" crosses the spread and always
# fills; "limit" rests near mid to avoid paying the spread and auto-cancels if
# unfilled after LIMIT_TTL_BARS.
ENTRY_ORDER_TYPE = os.getenv("ENTRY_ORDER_TYPE", "market").strip().lower()
LIMIT_ANCHOR = os.getenv("LIMIT_ANCHOR", "mid").strip().lower()  # mid | bid | ask
LIMIT_OFFSET_TICKS = int(os.getenv("LIMIT_OFFSET_TICKS", "0"))
LIMIT_TTL_BARS = max(1, int(os.getenv("LIMIT_TTL_BARS", "2")))

# Which strategies to enable at boot: "all", "none"/"", or a comma-separated
# slug list. Defaults to all when AUTOSTART is on (an autostarted engine that
# trades nothing would be surprising), none otherwise.
ENABLED_STRATEGIES = os.getenv(
    "ENGINE_ENABLED_STRATEGIES", "all" if AUTOSTART else "none"
).strip().lower()

# Baseline for the engine's SESSION P&L tracker (USD). This is not a wallet and
# never was a real balance — it is the starting point of an equity line so the
# UI can show "P&L since the engine started". The authoritative demo-account
# balance comes from GET /api/account, which reads the exchange directly.
SESSION_EQUITY_BASE = float(os.getenv("SESSION_EQUITY_BASE",
                                      os.getenv("PAPER_START_BALANCE", "10000")))
# Back-compat alias: store.performance() uses this as the equity-curve origin.
PAPER_START_BALANCE = SESSION_EQUITY_BASE

# Delta taker fee for options: 0.03% of notional, capped at 3.5% of premium,
# plus 18% GST. Applied to paper P&L so results are realistic (matches the
# fee model verified in the earlier autotrader backtests).
FEE_NOTIONAL_RATE = 0.0003
FEE_PREMIUM_CAP = 0.035
GST_RATE = 0.18

# Safety: never hold more than this many open positions across all strategies.
MAX_OPEN_POSITIONS = int(os.getenv("ENGINE_MAX_OPEN", "8"))

# Never trade an expiry with less life than this. Delta settles options at
# 12:00 UTC; in the final hours the remaining premium is almost entirely theta
# and the book thins out, so an entry there is structurally losing and there is
# nothing to exit into. Without this floor the resolver happily returned an
# expiry minutes away (audit #5).
MIN_HOURS_TO_EXPIRY = float(os.getenv("ENGINE_MIN_HOURS_TO_EXPIRY", "2"))

# An orphan is size on the exchange that the engine is not tracking: it has no
# stop-loss, no target and no time exit, so it rides to settlement unmanaged.
# It appears after an ambiguous fill, a manual trade in the Delta UI, or a
# restart with positions still open.
#
#   adopt   -> bring it under management with the standard stop/target, so the
#              engine's book matches the account. Default: the point of syncing
#              is that the engine manages everything the account holds.
#   flatten -> close it with a reduce_only sell (previous behaviour).
#   report  -> journal it and do nothing. Leaves REAL risk unmanaged.
ORPHAN_POLICY = os.getenv("ENGINE_ORPHAN_POLICY", "adopt").strip().lower()
if ORPHAN_POLICY not in ("adopt", "flatten", "report"):
    ORPHAN_POLICY = "adopt"
# Back-compat: the old boolean still forces flatten when explicitly set.
if os.getenv("ENGINE_AUTO_FLATTEN_ORPHANS", "").strip().lower() in {"1", "true", "yes"}:
    ORPHAN_POLICY = "flatten"
AUTO_FLATTEN_ORPHANS = ORPHAN_POLICY == "flatten"

# Stop / target applied to an ADOPTED position, as a fraction of its entry
# premium. Adoption cannot know which strategy opened the position or what its
# intent was, so it uses a single conservative bracket rather than guessing.
ADOPT_SL_PCT = float(os.getenv("ENGINE_ADOPT_SL_PCT", "0.30"))
ADOPT_RR = float(os.getenv("ENGINE_ADOPT_RR", "1.5"))

# How often the account sync polls the exchange (seconds). Runs whether or not
# the engine is running, so a stopped engine still shows a truthful account.
ACCOUNT_SYNC_SECONDS = max(5.0, float(os.getenv("ENGINE_ACCOUNT_SYNC_SECONDS", "15")))

# Close live positions when the app shuts down. A process exit otherwise leaves
# real testnet positions with no manager watching their stops.
FLATTEN_ON_SHUTDOWN = os.getenv(
    "ENGINE_FLATTEN_ON_SHUTDOWN", "true"
).strip().lower() in {"1", "true", "yes"}

# Max bars a position may be held before a time-based exit (per strategy tf).
DEFAULT_MAX_HOLD_BARS = int(os.getenv("ENGINE_MAX_HOLD_BARS", "60"))

# After a (strategy, direction) position closes, block re-entry on that same
# pair for this many bars so a persisting condition can't machine-gun trades.
COOLDOWN_BARS = int(os.getenv("ENGINE_COOLDOWN_BARS", "5"))

# --------------------------------------------------------------------------- #
# AI strategy (LLM-driven signals)
# --------------------------------------------------------------------------- #
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://integrate.api.nvidia.com/v1").strip()
AI_MODEL = os.getenv("AI_MODEL", "z-ai/glm-5.2").strip()
# Tried in order if the primary model times out or errors. Several NVIDIA-hosted
# models (incl. z-ai/glm-5.2 and llama-3.3-70b as of 2026-07-21) accept the
# connection but never return, so a fallback keeps the strategy alive.
AI_FALLBACK_MODELS = [
    m.strip() for m in os.getenv(
        "AI_FALLBACK_MODELS", "abacusai/dracarys-llama-3.1-70b-instruct"
    ).split(",") if m.strip()
]
AI_TEMPERATURE = float(os.getenv("AI_TEMPERATURE", "0.2"))
AI_REFRESH_SECONDS = int(os.getenv("AI_REFRESH_SECONDS", "300"))
AI_MIN_CONFIDENCE = float(os.getenv("AI_MIN_CONFIDENCE", "0.65"))
AI_TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS", "45"))

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
        "live_only": False,
        "paper_mode_available": True,
        "session_equity_base": SESSION_EQUITY_BASE,
        "has_keys": False,
        "entry_order_type": ENTRY_ORDER_TYPE,
        "limit_anchor": LIMIT_ANCHOR,
    }
