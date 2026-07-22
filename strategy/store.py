"""SQLite persistence + analytics for completed trades (Phase 5).

The in-memory journal (journal.py) is a short ring for the live feed; this is
the durable record. Every realized close (full or partial) is written here as
one row, and the performance page reads its aggregates from this table.

Thread-safe: the engine worker thread writes while API-route threads read, so
every operation uses its own short-lived connection guarded by a write lock.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from typing import Optional

from . import config

DB_PATH = config.JOURNAL_DIR / "trades.db"
_write_lock = threading.Lock()


@contextlib.contextmanager
def _conn():
    """Short-lived connection that both transacts AND closes.

    `with sqlite3.connect(...) as c` commits/rolls back but does NOT close the
    connection — every read path was leaking a file handle until GC reclaimed it,
    and health_publisher opens two of those every 5 seconds (audit #20).
    """
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        with c:              # transaction scope
            yield c
    finally:
        c.close()            # always release the handle


def init_db() -> None:
    with _write_lock, _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_open       REAL,
                ts_close      REAL,
                strategy      TEXT,
                direction     TEXT,
                symbol        TEXT,
                strike        REAL,
                expiry        TEXT,
                entry_price   REAL,
                exit_price    REAL,
                contracts     INTEGER,
                contract_value REAL,
                gross_pnl     REAL,
                net_pnl       REAL,
                why           TEXT,
                mode          TEXT,
                partial       INTEGER DEFAULT 0,
                confidence    INTEGER DEFAULT 50,
                leverage      INTEGER DEFAULT 1
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_close ON trades(ts_close)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy)")
        # Implied-vol snapshots — accumulates the forward series needed to test
        # the variance risk premium properly (IV today vs realized vol later).
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS iv_snapshots (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       REAL,
                asset    TEXT,
                expiry   TEXT,
                dte      REAL,
                atm_iv   REAL,
                spot     REAL,
                rv7      REAL,
                rv14     REAL,
                rv30     REAL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_iv_ts ON iv_snapshots(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_iv_expiry ON iv_snapshots(expiry)")
        # Migration: skew columns added after the table shipped. SQLite has no
        # ADD COLUMN IF NOT EXISTS, so check the existing schema first.
        have = {r["name"] for r in c.execute("PRAGMA table_info(iv_snapshots)")}
        for col in ("iv_25d_call", "iv_25d_put", "rr25", "bf25"):
            if col not in have:
                c.execute(f"ALTER TABLE iv_snapshots ADD COLUMN {col} REAL")

        have_trades = {r["name"] for r in c.execute("PRAGMA table_info(trades)")}
        if "confidence" not in have_trades:
            c.execute("ALTER TABLE trades ADD COLUMN confidence INTEGER DEFAULT 50")
        if "leverage" not in have_trades:
            c.execute("ALTER TABLE trades ADD COLUMN leverage INTEGER DEFAULT 1")


def record_trade(row: dict) -> None:
    cols = (
        "ts_open", "ts_close", "strategy", "direction", "symbol", "strike",
        "expiry", "entry_price", "exit_price", "contracts", "contract_value",
        "gross_pnl", "net_pnl", "why", "mode", "partial", "confidence", "leverage"
    )
    try:
        with _write_lock, _conn() as c:
            c.execute(
                f"INSERT INTO trades ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                tuple(row.get(k) for k in cols),
            )
    except Exception as exc:  # noqa: BLE001 — persistence must never crash the engine
        print(f"[store] write failed: {exc!r}")


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def fetch_trades(
    limit: int = 200,
    offset: int = 0,
    strategy: Optional[str] = None,
    outcome: Optional[str] = None,
    mode: Optional[str] = None,
) -> list[dict]:
    where, params = [], []
    if strategy and strategy != "all":
        where.append("strategy = ?")
        params.append(strategy)
    if mode and mode != "all":
        where.append("mode = ?")
        params.append(mode)
    if outcome == "win":
        where.append("net_pnl > 0")
    elif outcome == "loss":
        where.append("net_pnl < 0")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM trades {clause} ORDER BY ts_close DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def count_trades() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]


# Realized-P&L summary, cached. `stats()` is published to every dashboard once
# a second from the event loop, so this must not hit SQLite on every call.
_SUMMARY_TTL = 5.0
_summary_cache: tuple[float, dict] = (0.0, {})


def realized_summary(mode: Optional[str] = "live_demo", force: bool = False) -> dict:
    """Durable realized P&L for the given execution mode.

    The executor's in-memory counters (wins / losses / realized_pnl) reset on
    every process restart, so a dashboard reading them shows zeros next to an
    account that plainly has a trade history. This reads the SQLite record
    instead, which is the only thing that survives a restart.

    Defaults to `live_demo` so historical paper rows cannot inflate or deflate
    the P&L shown against a live account.
    """
    global _summary_cache
    now = time.time()
    key = mode or "all"
    ts, cached = _summary_cache
    if not force and cached.get("_key") == key and (now - ts) < _SUMMARY_TTL:
        return cached

    where, params = "", ()
    if mode:
        where, params = "WHERE mode = ?", (mode,)
    with _conn() as c:
        row = c.execute(
            f"""SELECT COUNT(*) n,
                       COALESCE(SUM(net_pnl), 0)                      net,
                       COALESCE(SUM(gross_pnl), 0)                    gross,
                       COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END), 0) wins,
                       COALESCE(SUM(CASE WHEN net_pnl < 0 THEN 1 ELSE 0 END), 0) losses,
                       COALESCE(SUM(CASE WHEN net_pnl > 0 THEN net_pnl ELSE 0 END), 0) gross_win,
                       COALESCE(SUM(CASE WHEN net_pnl < 0 THEN -net_pnl ELSE 0 END), 0) gross_loss,
                       MAX(ts_close) last_close
                FROM trades {where}""", params).fetchone()
    n = row["n"] or 0
    wins, losses = row["wins"] or 0, row["losses"] or 0
    decided = wins + losses
    out = {
        "_key": key,
        "mode": mode or "all",
        "closed_trades": n,
        "realized_pnl": round(row["net"] or 0.0, 4),
        "gross_pnl": round(row["gross"] or 0.0, 4),
        "wins": wins,
        "losses": losses,
        "win_rate": round(100.0 * wins / decided, 1) if decided else None,
        "profit_factor": (round((row["gross_win"] or 0) / (row["gross_loss"] or 1), 3)
                          if (row["gross_loss"] or 0) > 0 else None),
        "last_close_ts": row["last_close"],
    }
    _summary_cache = (now, out)
    return out


def purge_trades(strategies: tuple[str, ...] = (), symbols: tuple[str, ...] = ()) -> int:
    """Delete rows by strategy and/or symbol. Used to remove rows written by
    test harnesses that exercised the real Executor against a stubbed client —
    those are not real fills and must not pollute realized P&L."""
    clauses, params = [], []
    if strategies:
        clauses.append("strategy IN (%s)" % ",".join("?" * len(strategies)))
        params.extend(strategies)
    if symbols:
        clauses.append("symbol IN (%s)" % ",".join("?" * len(symbols)))
        params.extend(symbols)
    if not clauses:
        return 0
    with _write_lock, _conn() as c:
        cur = c.execute("DELETE FROM trades WHERE " + " OR ".join(clauses), params)
        n = cur.rowcount
    global _summary_cache
    _summary_cache = (0.0, {})
    return n


def performance() -> dict:
    """Aggregate metrics, equity curve, and per-strategy breakdown."""
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT strategy, net_pnl, gross_pnl, ts_close, mode FROM trades ORDER BY ts_close ASC"
        ).fetchall()]

    start_balance = config.PAPER_START_BALANCE
    equity = start_balance
    peak = start_balance
    max_dd = 0.0
    curve: list[dict] = []
    gross_win = 0.0
    gross_loss = 0.0
    wins = losses = 0
    best = worst = None

    per: dict[str, dict] = {}

    for r in rows:
        net = r["net_pnl"] or 0.0
        equity += net
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        curve.append({"t": r["ts_close"], "equity": round(equity, 4), "net": round(net, 4)})

        if net > 0:
            wins += 1
            gross_win += net
        elif net < 0:
            losses += 1
            gross_loss += -net
        best = net if best is None else max(best, net)
        worst = net if worst is None else min(worst, net)

        p = per.setdefault(r["strategy"], {"strategy": r["strategy"], "trades": 0,
                                           "wins": 0, "losses": 0, "net_pnl": 0.0})
        p["trades"] += 1
        p["net_pnl"] += net
        if net > 0:
            p["wins"] += 1
        elif net < 0:
            p["losses"] += 1

    total = wins + losses
    per_list = sorted(per.values(), key=lambda x: x["net_pnl"], reverse=True)
    for p in per_list:
        t = p["wins"] + p["losses"]
        p["win_rate"] = round(p["wins"] / t * 100, 1) if t else None
        p["net_pnl"] = round(p["net_pnl"], 4)

    return {
        "start_balance": start_balance,
        "equity": round(equity, 4),
        "total_pnl": round(equity - start_balance, 4),
        "total_trades": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total * 100, 1) if total else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "avg_win": round(gross_win / wins, 4) if wins else None,
        "avg_loss": round(-gross_loss / losses, 4) if losses else None,
        "max_drawdown": round(max_dd, 4),
        "best_trade": round(best, 4) if best is not None else None,
        "worst_trade": round(worst, 4) if worst is not None else None,
        "equity_curve": curve,
        "per_strategy": per_list,
    }


def record_iv_snapshot(rows: list[dict]) -> int:
    """Persist one VRP snapshot (one row per expiry)."""
    cols = ("ts", "asset", "expiry", "dte", "atm_iv", "spot", "rv7", "rv14", "rv30",
            "iv_25d_call", "iv_25d_put", "rr25", "bf25")
    if not rows:
        return 0
    try:
        with _write_lock, _conn() as c:
            c.executemany(
                f"INSERT INTO iv_snapshots ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [tuple(r.get(k) for k in cols) for r in rows],
            )
        return len(rows)
    except Exception as exc:  # noqa: BLE001 — never crash the caller
        print(f"[store] iv snapshot write failed: {exc!r}")
        return 0


def iv_snapshot_stats() -> dict:
    """How much forward IV data has accumulated so far."""
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) n, MIN(ts) first_ts, MAX(ts) last_ts,"
            " COUNT(DISTINCT expiry) expiries FROM iv_snapshots"
        ).fetchone()
    n, first_ts, last_ts, expiries = row["n"], row["first_ts"], row["last_ts"], row["expiries"]
    span_days = round((last_ts - first_ts) / 86400, 2) if (first_ts and last_ts) else 0
    return {"rows": n, "first_ts": first_ts, "last_ts": last_ts,
            "expiries": expiries, "span_days": span_days}


def iv_history(limit: int = 500) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM iv_snapshots ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def strategies_seen() -> list[str]:
    with _conn() as c:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT strategy FROM trades ORDER BY strategy"
        ).fetchall()]


# --------------------------------------------------------------------------- #
# One-time backfill from the legacy JSONL journal so historical closes appear.
# --------------------------------------------------------------------------- #
def backfill_from_jsonl() -> int:
    if count_trades() > 0:
        return 0
    path = config.JOURNAL_DIR / "trades.jsonl"
    if not path.exists():
        return 0
    import json
    n = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if e.get("kind") not in ("close", "close_partial"):
                continue
            record_trade({
                "ts_open": None,
                "ts_close": e.get("ts"),
                "strategy": e.get("strategy"),
                "direction": e.get("direction"),
                "symbol": e.get("symbol"),
                "strike": None,
                "expiry": None,
                "entry_price": e.get("entry_price"),
                "exit_price": e.get("exit_price"),
                "contracts": e.get("contracts"),
                "contract_value": None,
                "gross_pnl": e.get("gross_pnl"),
                "net_pnl": e.get("net_pnl"),
                "why": e.get("why"),
                "mode": e.get("mode"),
                "partial": 1 if e.get("kind") == "close_partial" else 0,
            })
            n += 1
    return n


init_db()
