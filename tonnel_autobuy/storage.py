"""SQLite storage (aiosqlite): seen listings, purchases, rules, settings, plus the honest-price tables
(floor_snapshots, observations, decisions), deal cards (pending, mutes) and the practice ledger (marks).
Old scaffold databases are migrated in place (guarded ALTER TABLE ... ADD COLUMN): rules and purchases
are kept."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from .models import Listing, alnum_key, norm
from .rules import STATE_UNSET, BuyRule, file_rule_keys

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    listing_id TEXT NOT NULL,
    price REAL NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (listing_id, price)
);
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL,
    collection TEXT NOT NULL,
    model TEXT, backdrop TEXT, symbol TEXT, number INTEGER,
    price_ton REAL NOT NULL,
    cost_ton REAL NOT NULL,
    floor_ton REAL,
    rule_id INTEGER,
    dry INTEGER NOT NULL,
    status TEXT NOT NULL,          -- dry | ok | failed | unknown
    message TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS purchases_ts ON purchases (ts);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection TEXT NOT NULL,
    model TEXT, backdrop TEXT, symbol TEXT,
    max_price_ton REAL NOT NULL,
    min_discount_pct REAL NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Tables added after the scaffold (created on every open; pruned on start).
NEW_TABLES = """
CREATE TABLE IF NOT EXISTS floor_snapshots (
    ts REAL NOT NULL,
    filter_key TEXT NOT NULL,
    floor_ton REAL,
    n INTEGER,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_key_ts ON floor_snapshots(filter_key, ts);

CREATE TABLE IF NOT EXISTS observations (
    listing_id TEXT NOT NULL,
    price_ton REAL NOT NULL,
    collection TEXT,
    model TEXT,
    backdrop TEXT,
    symbol TEXT,
    number INTEGER,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY (listing_id, price_ton)
);
CREATE INDEX IF NOT EXISTS idx_obs_first ON observations(first_seen);
CREATE INDEX IF NOT EXISTS idx_obs_coll ON observations(collection, number);

CREATE TABLE IF NOT EXISTS decisions (
    ts REAL NOT NULL,
    listing_id TEXT NOT NULL,
    price_ton REAL,
    rule_id INTEGER,
    outcome TEXT NOT NULL,          -- skip | alert | dry | bought | failed | muted | near | tap
    reason_code TEXT,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_listing ON decisions(listing_id, ts);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS pending (
    token TEXT PRIMARY KEY,
    listing_id TEXT,
    rule_id INTEGER,
    price_ton REAL,
    cost_ton REAL,
    payload_json TEXT,
    msgs_json TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL            -- pending|executing|bought|dry|gone|changed|expired|skipped|watch|near
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending(status, expires_at);

CREATE TABLE IF NOT EXISTS mutes (
    key TEXT PRIMARY KEY,
    until_ts REAL NOT NULL
);

-- Persisted safety switches (suite contract): kv.panic = 1 after /panic, survives restarts.
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Shadow-first rules: one row per buy a shadow rule would have made (it never reaches the executor),
-- revalued each tick by shadow.mark() net of fees. outcome: open | gain | loss | flat | sniped.
CREATE TABLE IF NOT EXISTS shadow_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER,
    subject_ref TEXT NOT NULL,
    ts REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_costs REAL NOT NULL,
    executable_price REAL NOT NULL,
    mark_price REAL,
    mark_ts REAL,
    mark_source TEXT,
    outcome TEXT NOT NULL DEFAULT 'open',
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_shadow_rule_ts ON shadow_fills(rule_id, ts);

CREATE TABLE IF NOT EXISTS marks (
    purchase_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    floor_ton REAL,
    net_exit_ton REAL,
    confidence TEXT,
    PRIMARY KEY (purchase_id, ts)
);
"""

# Columns added after the scaffold: (table, column, declaration). Applied with guarded ALTERs.
# purchases already had cost_ton, model, backdrop and symbol in the Tonnel scaffold.
ADDED_COLUMNS = (
    ("rules", "source", "TEXT NOT NULL DEFAULT 'bot'"),
    ("rules", "min_profit_ton", "REAL NOT NULL DEFAULT 0"),
    ("rules", "min_profit_pct", "REAL NOT NULL DEFAULT 0"),
    ("rules", "min_confidence", "TEXT NOT NULL DEFAULT 'med'"),
    ("rules", "allow_locked_days", "REAL NOT NULL DEFAULT 0"),
    ("rules", "mode", "TEXT NOT NULL DEFAULT 'auto'"),
    ("rules", "exit", "TEXT NOT NULL DEFAULT 'here'"),
    ("rules", "state", "TEXT NOT NULL DEFAULT 'live'"),
    ("rules", "promoted_at", "REAL"),
    ("purchases", "closed_at", "REAL"),
    ("purchases", "exit_price_ton", "REAL"),
    ("purchases", "exit_note", "TEXT"),
)

RULE_FIELDS = (
    "collection", "model", "backdrop", "symbol", "max_price_ton", "min_discount_pct", "enabled", "source",
    "min_profit_ton", "min_profit_pct", "min_confidence", "allow_locked_days", "mode", "exit", "state", "promoted_at",
)

# Statuses that count against budget / hourly limits ("unknown" = maybe bought: a timeout, network error
# or 5xx may have gone through on the server, so it counts until proven otherwise).
SPENT_STATUSES = ("dry", "ok", "unknown")
# Positions the practice ledger marks and reports (Tonnel also counts 'unknown': it may be in your wallet).
POSITION_STATUSES = ("dry", "ok", "unknown")
_POS_IN = "(" + ",".join(f"'{x}'" for x in POSITION_STATUSES) + ")"


@dataclass(slots=True)
class Snapshot:
    ts: float
    filter_key: str
    floor_ton: float | None
    n: int
    source: str


@dataclass(slots=True)
class Decision:
    ts: float
    listing_id: str
    price_ton: float | None
    rule_id: int | None
    outcome: str
    reason_code: str | None
    detail: dict[str, Any]


@dataclass(slots=True)
class PendingCard:
    token: str
    listing_id: str
    rule_id: int | None
    price_ton: float
    cost_ton: float
    payload: dict[str, Any]
    msgs: list[tuple[int, int]]
    created_at: float
    expires_at: float
    status: str


@dataclass(slots=True)
class Position:
    id: int
    listing_id: str
    collection: str
    model: str | None
    title: str
    price_ton: float
    cost_ton: float
    rule_id: int | None
    dry: bool
    status: str
    created_at: float
    closed_at: float | None
    exit_price_ton: float | None
    mark_ts: float | None = None
    mark_floor: float | None = None
    mark_net: float | None = None
    mark_conf: str | None = None


def utc_day_start(now: float | None = None) -> float:
    dt = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Storage:
    def __init__(self, path: str):
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> "Storage":
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.executescript(NEW_TABLES)
        await self._migrate()
        await self._db.commit()
        return self

    async def _columns(self, table: str) -> set[str]:
        async with self.db.execute(f"PRAGMA table_info({table})") as cur:
            return {row[1] for row in await cur.fetchall()}

    async def _migrate(self) -> None:
        """Guarded ALTER TABLE ... ADD COLUMN for scaffold databases; keeps rules and purchases. A panic flag
        stored in `settings` by an earlier build moves to `kv`."""
        await self.db.execute("INSERT OR IGNORE INTO kv (key, value) SELECT key, value FROM settings WHERE key='panic'")
        await self.db.execute("DELETE FROM settings WHERE key='panic'")
        cache: dict[str, set[str]] = {}
        for table, column, decl in ADDED_COLUMNS:
            if table not in cache:
                cache[table] = await self._columns(table)
            if column not in cache[table]:
                await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                cache[table].add(column)

    async def prune(self, now: float, *, obs_days: int = 14, snap_days: int = 14, decision_days: int = 7) -> None:
        await self.db.execute("DELETE FROM floor_snapshots WHERE ts < ?", (now - snap_days * 86400,))
        await self.db.execute("DELETE FROM observations WHERE last_seen < ?", (now - obs_days * 86400,))
        await self.db.execute("DELETE FROM decisions WHERE ts < ?", (now - decision_days * 86400,))
        await self.db.execute("DELETE FROM mutes WHERE until_ts < ?", (now,))
        await self.db.execute("DELETE FROM pending WHERE created_at < ?", (now - 7 * 86400,))
        await self.db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage is not open")
        return self._db

    # ------------------------------------------------------------ seen

    async def mark_seen_one(self, listing: Listing) -> bool:
        """Insert one (id, price); True if it was new (atomic dedupe before evaluation)."""
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO seen (listing_id, price, ts) VALUES (?, ?, ?)",
            (listing.id, listing.price_ton, time.time()),
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def unmark_seen(self, listing: Listing) -> None:
        """Let a listing be evaluated again (a pause landed before the engine acted on it)."""
        await self.db.execute("DELETE FROM seen WHERE listing_id=? AND price=?", (listing.id, listing.price_ton))
        await self.db.commit()

    async def prune_seen(self, older_than_s: float = 7 * 86400) -> int:
        cur = await self.db.execute("DELETE FROM seen WHERE ts < ?", (time.time() - older_than_s,))
        await self.db.commit()
        return cur.rowcount

    # ------------------------------------------------------------ purchases

    async def add_purchase(self, listing: Listing, *, cost_ton: float, floor_ton: float | None,
                           rule_id: int | None, dry: bool, status: str, message: str = "",
                           created_at: float | None = None) -> int:
        cur = await self.db.execute(
            "INSERT INTO purchases (listing_id, collection, model, backdrop, symbol, number,"
            " price_ton, cost_ton, floor_ton, rule_id, dry, status, message, ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (listing.id, listing.collection, listing.model, listing.backdrop, listing.symbol,
             listing.number, listing.price_ton, cost_ton, floor_ton, rule_id, int(dry), status,
             message, time.time() if created_at is None else created_at),
        )
        await self.db.commit()
        return cur.lastrowid

    async def set_purchase_status(self, purchase_id: int, status: str, message: str) -> None:
        await self.db.execute("UPDATE purchases SET status=?, message=? WHERE id=?", (status, message, purchase_id))
        await self.db.commit()

    async def was_bought(self, listing_id: str, dry: bool) -> bool:
        q = "SELECT 1 FROM purchases WHERE listing_id=? AND dry=? AND status IN (%s)" % (
            ",".join("?" * len(SPENT_STATUSES)))
        cur = await self.db.execute(q, (listing_id, int(dry), *SPENT_STATUSES))
        return await cur.fetchone() is not None

    async def spent_since(self, since_ts: float, dry: bool) -> float:
        q = "SELECT COALESCE(SUM(cost_ton), 0) FROM purchases WHERE ts>=? AND dry=? AND status IN (%s)" % (
            ",".join("?" * len(SPENT_STATUSES)))
        cur = await self.db.execute(q, (since_ts, int(dry), *SPENT_STATUSES))
        row = await cur.fetchone()
        return float(row[0])

    async def count_since(self, since_ts: float, dry: bool) -> int:
        q = "SELECT COUNT(*) FROM purchases WHERE ts>=? AND dry=? AND status IN (%s)" % (
            ",".join("?" * len(SPENT_STATUSES)))
        cur = await self.db.execute(q, (since_ts, int(dry), *SPENT_STATUSES))
        row = await cur.fetchone()
        return int(row[0])

    async def buys_since(self, since_ts: float, dry: bool) -> int:
        return await self.count_since(since_ts, dry)

    async def recent_purchases(self, limit: int = 10) -> list[dict]:
        cur = await self.db.execute("SELECT * FROM purchases ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------ practice ledger

    @staticmethod
    def _title(r: aiosqlite.Row) -> str:
        num = f" #{r['number']}" if r["number"] is not None else ""
        attrs = " / ".join(a for a in (r["model"], r["backdrop"], r["symbol"]) if a)
        return f"{r['collection']}{num}" + (f" [{attrs}]" if attrs else "")

    def _position(self, r: aiosqlite.Row) -> Position:
        keys = r.keys()
        return Position(
            id=r["id"],
            listing_id=r["listing_id"],
            collection=r["collection"] or "",
            model=r["model"],
            title=self._title(r),
            price_ton=r["price_ton"],
            cost_ton=r["cost_ton"] if r["cost_ton"] is not None else r["price_ton"],
            rule_id=r["rule_id"],
            dry=bool(r["dry"]),
            status=r["status"],
            created_at=r["ts"],
            closed_at=r["closed_at"],
            exit_price_ton=r["exit_price_ton"],
            mark_ts=r["mark_ts"] if "mark_ts" in keys else None,
            mark_floor=r["mark_floor"] if "mark_floor" in keys else None,
            mark_net=r["mark_net"] if "mark_net" in keys else None,
            mark_conf=r["mark_conf"] if "mark_conf" in keys else None,
        )

    _POSITIONS_SQL = (
        "SELECT p.*, m.ts AS mark_ts, m.floor_ton AS mark_floor, m.net_exit_ton AS mark_net, m.confidence AS mark_conf"
        " FROM purchases p LEFT JOIN marks m ON m.purchase_id = p.id"
        " AND m.ts = (SELECT MAX(ts) FROM marks WHERE purchase_id = p.id)"
    )

    async def positions(self, *, since: float | None = None, dry: bool | None = None) -> list[Position]:
        """Buys that may be in your wallet (dry, ok, unknown) with their latest mark."""
        sql = self._POSITIONS_SQL + f" WHERE p.status IN {_POS_IN}"
        args: list[Any] = []
        if since is not None:
            sql += " AND p.ts >= ?"
            args.append(since)
        if dry is not None:
            sql += " AND p.dry = ?"
            args.append(int(dry))
        async with self.db.execute(sql + " ORDER BY p.id", args) as cur:
            return [self._position(r) for r in await cur.fetchall()]

    async def open_positions(self, now: float, paper_horizon_days: float = 7) -> list[Position]:
        sql = (
            self._POSITIONS_SQL + f" WHERE p.status IN {_POS_IN} AND p.closed_at IS NULL"
            " AND (p.dry = 0 OR p.ts >= ?) ORDER BY p.id"
        )
        async with self.db.execute(sql, (now - paper_horizon_days * 86400,)) as cur:
            return [self._position(r) for r in await cur.fetchall()]

    async def get_position(self, purchase_id: int) -> Position | None:
        async with self.db.execute(self._POSITIONS_SQL + " WHERE p.id = ?", (purchase_id,)) as cur:
            row = await cur.fetchone()
        return self._position(row) if row else None

    async def close_position(self, purchase_id: int, exit_price: float, note: str, now: float) -> bool:
        cur = await self.db.execute(
            f"UPDATE purchases SET closed_at=?, exit_price_ton=?, exit_note=? WHERE id=? AND status IN {_POS_IN}",
            (now, exit_price, note, purchase_id),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def add_mark(self, purchase_id: int, ts: float, floor: float, net_exit: float, confidence: str) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO marks(purchase_id, ts, floor_ton, net_exit_ton, confidence) VALUES (?,?,?,?,?)",
            (purchase_id, ts, floor, net_exit, confidence),
        )
        await self.db.commit()

    async def last_live_positions(self, n: int) -> list[Position]:
        sql = self._POSITIONS_SQL + " WHERE p.status = 'ok' AND p.dry = 0 ORDER BY p.id DESC LIMIT ?"
        async with self.db.execute(sql, (n,)) as cur:
            return [self._position(r) for r in await cur.fetchall()]

    # --- floor snapshots ------------------------------------------------------------

    async def add_snapshot(self, ts: float, filter_key: str, floor: float | None, n: int, source: str) -> None:
        await self.db.execute(
            "INSERT INTO floor_snapshots(ts, filter_key, floor_ton, n, source) VALUES (?,?,?,?,?)",
            (ts, filter_key, floor, n, source),
        )
        await self.db.commit()

    async def snapshots(self, filter_key: str, since: float, until: float | None = None) -> list[Snapshot]:
        sql = "SELECT * FROM floor_snapshots WHERE filter_key=? AND ts >= ?"
        args: list[Any] = [filter_key, since]
        if until is not None:
            sql += " AND ts <= ?"
            args.append(until)
        async with self.db.execute(sql + " ORDER BY ts", args) as cur:
            rows = await cur.fetchall()
        return [Snapshot(r["ts"], r["filter_key"], r["floor_ton"], r["n"] or 0, r["source"] or "") for r in rows]

    async def latest_snapshot(self, filter_key: str, newer_than: float = 0.0, before: float | None = None) -> Snapshot | None:
        sql = "SELECT * FROM floor_snapshots WHERE filter_key=? AND ts >= ? AND floor_ton IS NOT NULL"
        args: list[Any] = [filter_key, newer_than]
        if before is not None:
            sql += " AND ts <= ?"
            args.append(before)
        async with self.db.execute(sql + " ORDER BY ts DESC LIMIT 1", args) as cur:
            r = await cur.fetchone()
        return Snapshot(r["ts"], r["filter_key"], r["floor_ton"], r["n"] or 0, r["source"] or "") if r else None

    async def all_snapshots(self, since: float) -> list[Snapshot]:
        async with self.db.execute("SELECT * FROM floor_snapshots WHERE ts >= ? ORDER BY ts", (since,)) as cur:
            rows = await cur.fetchall()
        return [Snapshot(r["ts"], r["filter_key"], r["floor_ton"], r["n"] or 0, r["source"] or "") for r in rows]

    # --- observations ---------------------------------------------------------------

    async def upsert_observations(self, listings: list[Listing], now: float) -> None:
        await self.db.executemany(
            "INSERT INTO observations(listing_id, price_ton, collection, model, backdrop, symbol, number, first_seen,"
            " last_seen) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(listing_id, price_ton) DO UPDATE SET"
            " last_seen=excluded.last_seen",
            [
                (l.id, l.price_ton, l.collection, l.model, l.backdrop, l.symbol, l.number, now, now)
                for l in listings
            ],
        )
        await self.db.commit()

    async def observations_since(self, since: float, collection: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM observations WHERE first_seen >= ?"
        args: list[Any] = [since]
        async with self.db.execute(sql + " ORDER BY first_seen", args) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        if collection is not None:
            rows = [r for r in rows if norm(r["collection"]) == norm(collection)]
        return rows

    async def observation_bounds(self) -> tuple[float | None, float | None]:
        async with self.db.execute("SELECT MIN(first_seen), MAX(last_seen) FROM observations") as cur:
            row = await cur.fetchone()
        return (row[0], row[1]) if row else (None, None)

    async def last_seen(self, listing_id: str) -> float | None:
        async with self.db.execute("SELECT MAX(last_seen) FROM observations WHERE listing_id=?", (listing_id,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def polled_after(self, collection: str | None, ts: float) -> bool:
        """True when any listing of `collection` was observed after `ts`, i.e. the bot was still polling that
        market. A listing missing from those later answers is gone; without them (paused, /panic, stopped)
        nothing can be said about it."""
        if not collection:
            return False
        async with self.db.execute(
            "SELECT 1 FROM observations WHERE collection=? AND last_seen>? LIMIT 1", (collection, ts)
        ) as cur:
            return await cur.fetchone() is not None

    async def listings_with_number(self, number: int) -> list[tuple[str, str]]:
        """(listing id, collection) of every observed listing with this gift number, in any collection."""
        async with self.db.execute(
            "SELECT DISTINCT listing_id, collection FROM observations WHERE number=?", (number,)
        ) as cur:
            return [(r["listing_id"], r["collection"]) for r in await cur.fetchall()]

    async def listing_ids_for(self, slug_name: str, number: int) -> list[str]:
        """Listing ids seen for a t.me/nft slug (alnum collection name + number)."""
        async with self.db.execute(
            "SELECT DISTINCT listing_id, collection FROM observations WHERE number=?", (number,)
        ) as cur:
            rows = await cur.fetchall()
        return [r["listing_id"] for r in rows if alnum_key(r["collection"]) == alnum_key(slug_name)]

    async def observed_models(self, collection: str, limit: int = 8) -> list[tuple[str, float, int]]:
        """(model, cheapest seen price, listings) for a collection, most-listed first."""
        async with self.db.execute(
            "SELECT model, collection, MIN(price_ton) AS lo, COUNT(DISTINCT listing_id) AS n FROM observations"
            " WHERE model IS NOT NULL GROUP BY collection, model"
        ) as cur:
            rows = [r for r in await cur.fetchall() if norm(r["collection"]) == norm(collection)]
        rows.sort(key=lambda r: (-r["n"], r["model"]))
        return [(r["model"], r["lo"], r["n"]) for r in rows[:limit]]

    # --- decisions ------------------------------------------------------------------

    async def add_decision(
        self, ts: float, listing_id: str, price: float | None, rule_id: int | None, outcome: str,
        reason_code: str | None, detail: dict[str, Any] | None = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO decisions(ts, listing_id, price_ton, rule_id, outcome, reason_code, detail_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (ts, listing_id, price, rule_id, outcome, reason_code, json.dumps(detail or {}, default=str)),
        )
        await self.db.commit()

    @staticmethod
    def _decision(r: aiosqlite.Row) -> Decision:
        try:
            detail = json.loads(r["detail_json"] or "{}")
        except ValueError:
            detail = {}
        return Decision(r["ts"], r["listing_id"], r["price_ton"], r["rule_id"], r["outcome"], r["reason_code"], detail)

    async def decisions_for(self, listing_id: str, limit: int = 10) -> list[Decision]:
        async with self.db.execute(
            "SELECT * FROM decisions WHERE listing_id=? ORDER BY ts DESC, rowid DESC LIMIT ?", (listing_id, limit)
        ) as cur:
            return [self._decision(r) for r in await cur.fetchall()]

    async def decisions_since(self, since: float, until: float | None = None) -> list[Decision]:
        sql, args = "SELECT * FROM decisions WHERE ts >= ?", [since]
        if until is not None:
            sql += " AND ts < ?"
            args.append(until)
        async with self.db.execute(sql + " ORDER BY ts", args) as cur:
            return [self._decision(r) for r in await cur.fetchall()]

    async def reason_counts(self, since: float, until: float | None = None) -> dict[str, int]:
        sql = "SELECT reason_code, COUNT(*) FROM decisions WHERE ts >= ? AND reason_code IS NOT NULL"
        args: list[Any] = [since]
        if until is not None:
            sql += " AND ts < ?"
            args.append(until)
        async with self.db.execute(sql + " AND outcome IN ('skip','muted') GROUP BY reason_code", args) as cur:
            return {r[0]: r[1] for r in await cur.fetchall()}

    # --- pending deal cards ---------------------------------------------------------

    async def create_pending(
        self, token: str, listing_id: str, rule_id: int | None, price: float, cost: float,
        payload: dict[str, Any], now: float, ttl: float, status: str = "pending",
    ) -> None:
        await self.db.execute(
            "INSERT INTO pending(token, listing_id, rule_id, price_ton, cost_ton, payload_json, msgs_json, created_at,"
            " expires_at, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (token, listing_id, rule_id, price, cost, json.dumps(payload, default=str), "[]", now, now + ttl, status),
        )
        await self.db.commit()

    @staticmethod
    def _pending(r: aiosqlite.Row) -> PendingCard:
        return PendingCard(
            token=r["token"], listing_id=r["listing_id"], rule_id=r["rule_id"], price_ton=r["price_ton"],
            cost_ton=r["cost_ton"], payload=json.loads(r["payload_json"] or "{}"),
            msgs=[tuple(m) for m in json.loads(r["msgs_json"] or "[]")], created_at=r["created_at"],
            expires_at=r["expires_at"], status=r["status"],
        )

    async def get_pending(self, token: str) -> PendingCard | None:
        async with self.db.execute("SELECT * FROM pending WHERE token=?", (token,)) as cur:
            row = await cur.fetchone()
        return self._pending(row) if row else None

    async def set_pending_msgs(self, token: str, msgs: list[tuple[int, int]]) -> None:
        await self.db.execute("UPDATE pending SET msgs_json=? WHERE token=?", (json.dumps(msgs), token))
        await self.db.commit()

    async def claim_pending(self, token: str, now: float) -> bool:
        """Atomic pending -> executing; False when expired or already handled (double tap safe)."""
        cur = await self.db.execute(
            "UPDATE pending SET status='executing' WHERE token=? AND status='pending' AND expires_at > ?",
            (token, now),
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def set_pending_status(self, token: str, status: str, *, only_from: tuple[str, ...] = ()) -> bool:
        sql, args = "UPDATE pending SET status=? WHERE token=?", [status, token]
        if only_from:
            sql += f" AND status IN ({','.join('?' * len(only_from))})"
            args.extend(only_from)
        cur = await self.db.execute(sql, args)
        await self.db.commit()
        return cur.rowcount > 0

    async def list_pending(self, statuses: tuple[str, ...] = ("pending",)) -> list[PendingCard]:
        sql = f"SELECT * FROM pending WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY created_at"
        async with self.db.execute(sql, statuses) as cur:
            return [self._pending(r) for r in await cur.fetchall()]

    async def expire_pending(self, now: float | None = None, *, rule_id: int | None = None) -> list[PendingCard]:
        """Mark due (or, with now=None, all) pending cards expired, optionally only one rule's; returns them for editing."""
        sql, args = "SELECT * FROM pending WHERE status='pending'", []
        if now is not None:
            sql += " AND expires_at <= ?"
            args.append(now)
        if rule_id is not None:
            sql += " AND rule_id = ?"
            args.append(rule_id)
        async with self.db.execute(sql, args) as cur:
            cards = [self._pending(r) for r in await cur.fetchall()]
        for card in cards:
            await self.db.execute("UPDATE pending SET status='expired' WHERE token=? AND status='pending'", (card.token,))
        await self.db.commit()
        return cards

    async def cancel_executing(self) -> int:
        """/panic: taps in flight (status executing) are flagged so their last check before buy() refuses."""
        cur = await self.db.execute("UPDATE pending SET status='expired' WHERE status='executing'")
        await self.db.commit()
        return cur.rowcount

    async def pending_by_message(self, chat_id: int, message_id: int) -> PendingCard | None:
        async with self.db.execute("SELECT * FROM pending ORDER BY created_at DESC LIMIT 500") as cur:
            for r in await cur.fetchall():
                card = self._pending(r)
                if (chat_id, message_id) in card.msgs:
                    return card
        return None

    # --- mutes ------------------------------------------------------------------------

    async def add_mute(self, key: str, until_ts: float) -> None:
        await self.db.execute(
            "INSERT INTO mutes(key, until_ts) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET until_ts=excluded.until_ts",
            (key, until_ts),
        )
        await self.db.commit()

    async def remove_mute(self, key: str) -> bool:
        cur = await self.db.execute("DELETE FROM mutes WHERE key=?", (key,))
        await self.db.commit()
        return cur.rowcount > 0

    async def muted_key(self, keys: list[str], now: float) -> str | None:
        if not keys:
            return None
        sql = f"SELECT key FROM mutes WHERE until_ts > ? AND key IN ({','.join('?' * len(keys))}) LIMIT 1"
        async with self.db.execute(sql, [now, *keys]) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def list_mutes(self, now: float) -> list[tuple[str, float]]:
        async with self.db.execute("SELECT key, until_ts FROM mutes WHERE until_ts > ? ORDER BY key", (now,)) as cur:
            return [(r[0], r[1]) for r in await cur.fetchall()]


    # ------------------------------------------------------------ rules

    @staticmethod
    def _row_to_rule(r: aiosqlite.Row) -> BuyRule:
        return BuyRule(
            id=r["id"], collection=r["collection"], model=r["model"], backdrop=r["backdrop"],
            symbol=r["symbol"], max_price_ton=r["max_price_ton"],
            min_discount_pct=r["min_discount_pct"], enabled=bool(r["enabled"]),
            source=r["source"] or "bot",
            min_profit_ton=r["min_profit_ton"] or 0.0,
            min_profit_pct=r["min_profit_pct"] or 0.0,
            min_confidence=r["min_confidence"] or "med",
            allow_locked_days=r["allow_locked_days"] or 0.0,
            mode=r["mode"] or "auto",
            exit=r["exit"] or "here",
            state=r["state"] or "live",
            promoted_at=r["promoted_at"],
        )

    async def list_rules(self, enabled_only: bool = False) -> list[BuyRule]:
        q = "SELECT * FROM rules" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
        cur = await self.db.execute(q)
        return [self._row_to_rule(r) for r in await cur.fetchall()]

    async def get_rule(self, rule_id: int) -> BuyRule | None:
        async with self.db.execute("SELECT * FROM rules WHERE id=?", (rule_id,)) as cur:
            row = await cur.fetchone()
        return self._row_to_rule(row) if row else None

    async def rule_created(self, rule_id: int) -> float | None:
        async with self.db.execute("SELECT created_ts FROM rules WHERE id=?", (rule_id,)) as cur:
            row = await cur.fetchone()
        return float(row[0]) if row else None

    async def add_rule(self, rule: BuyRule) -> int:
        values = [getattr(rule, f) for f in RULE_FIELDS]
        values[RULE_FIELDS.index("enabled")] = int(rule.enabled)
        cur = await self.db.execute(
            f"INSERT INTO rules ({', '.join(RULE_FIELDS)}, created_ts) VALUES ({', '.join('?' * len(RULE_FIELDS))}, ?)",
            (*values, time.time()),
        )
        await self.db.commit()
        rule.id = cur.lastrowid
        return rule.id

    async def update_rule(self, rule_id: int, **fields: Any) -> bool:
        bad = set(fields) - set(RULE_FIELDS)
        if bad:
            raise ValueError(f"unknown rule fields: {sorted(bad)}")
        if not fields:
            return False
        if "enabled" in fields:
            fields["enabled"] = int(bool(fields["enabled"]))
        sql = "UPDATE rules SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE id=?"
        cur = await self.db.execute(sql, (*fields.values(), rule_id))
        await self.db.commit()
        return cur.rowcount > 0

    async def delete_rule(self, rule_id: int) -> bool:
        cur = await self.db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        await self.db.commit()
        return cur.rowcount > 0

    async def sync_rules(self, rules: Iterable[BuyRule]) -> int:
        """Insert rules (from rules.json) that are not already present. Returns # added."""
        existing = {self._rule_sig(r) for r in await self.list_rules()}
        added = 0
        for rule in rules:
            if self._rule_sig(rule) not in existing:
                await self.add_rule(rule)
                existing.add(self._rule_sig(rule))
                added += 1
        return added

    @staticmethod
    def _rule_sig(r: BuyRule) -> tuple:
        return (*r.group_key(), float(r.max_price_ton), float(r.min_discount_pct))

    async def replace_rules_from_source(self, source: str, rules: list[BuyRule], *, new_state: str = "live") -> int:
        """rules.json is the source of truth for its own rules, and each one keeps its id across restarts.

        A file rule is matched to its stored row by its filter (collection, model, backdrop, symbol; a
        repeated filter is told apart by its order in the file) and updated in place; rules that left the
        file are deleted, new ones inserted. A scaffold database has no `source` column: there an identical
        rule (same filter, max and discount, source 'bot') is adopted instead of duplicated. Everything
        keyed by rule id (verdicts, the go-live gate, crash pauses, shadow clocks, cards) survives restarts.

        A rule whose file entry has no "state" (STATE_UNSET) starts in `new_state` (shadow when
        SHADOW_NEW_RULES) when it is new, and keeps its stored state (a [Go live] promotion, a pause) when it
        already exists. Only an explicit "state" in the file overwrites the stored one."""
        everything = await self.list_rules()
        stored = [r for r in everything if r.source == source]
        by_key = {key: r.id for r, key in zip(stored, file_rule_keys(stored)) if r.id is not None}
        legacy = {self._rule_sig(r): r.id for r in everything if r.source == "bot" and r.id is not None}
        keep: set[int] = set()
        for rule, key in zip(rules, file_rule_keys(rules)):
            rule.source = source
            rule_id = by_key.get(key)
            if rule_id is None and await self.get_setting("rules_adopted") is None:
                rule_id = legacy.pop(self._rule_sig(rule), None)
            state_unset = rule.state == STATE_UNSET
            if rule_id is None:
                if state_unset:
                    rule.state = new_state
                keep.add(await self.add_rule(rule))
                continue
            rule.id = rule_id
            skip = {"promoted_at"} | ({"state"} if state_unset else set())
            await self.update_rule(rule_id, **{f: getattr(rule, f) for f in RULE_FIELDS if f not in skip})
            if state_unset:
                stored_rule = await self.get_rule(rule_id)
                rule.state = stored_rule.state if stored_rule else new_state
            keep.add(rule_id)
        for rule_id in set(by_key.values()) - keep:
            await self.delete_rule(rule_id)
        await self.set_setting("rules_adopted", "1")
        return len(rules)

    # ------------------------------------------------------------ settings

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        cur = await self.db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default

    async def set_setting(self, key: str, value: Any) -> None:
        await self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        await self.db.commit()

    async def take_setting(self, key: str, value: str) -> bool:
        """Delete `key` only if it still holds `value`; True for exactly one caller (one-time tokens)."""
        cur = await self.db.execute("DELETE FROM settings WHERE key=? AND value=?", (key, value))
        await self.db.commit()
        return cur.rowcount == 1

    async def delete_setting(self, key: str) -> None:
        await self.db.execute("DELETE FROM settings WHERE key=?", (key,))
        await self.db.commit()

    async def get_float(self, key: str) -> float | None:
        value = await self.get_setting(key)
        try:
            return None if value is None else float(value)
        except ValueError:
            return None

    async def settings_with_prefix(self, prefix: str) -> dict[str, str]:
        async with self.db.execute("SELECT key, value FROM settings WHERE key LIKE ?", (prefix + "%",)) as cur:
            return {r[0]: r[1] for r in await cur.fetchall()}

    # ------------------------------------------------------------ kv (persisted safety switches)

    async def kv_get(self, key: str, default: str | None = None) -> str | None:
        async with self.db.execute("SELECT value FROM kv WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else default

    async def kv_set(self, key: str, value: Any) -> None:
        await self.db.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, str(value)))
        await self.db.commit()

    async def is_panic(self) -> bool:
        return await self.kv_get("panic", "0") == "1"

    async def set_panic(self, value: bool) -> None:
        await self.kv_set("panic", "1" if value else "0")

    # ------------------------------------------------------------ shadow fills

    async def add_shadow_fill(self, rule_id: int | None, subject_ref: str, ts: float, entry_price: float,
                              entry_costs: float, executable_price: float, meta: dict[str, Any]) -> int:
        cur = await self.db.execute(
            "INSERT INTO shadow_fills (rule_id, subject_ref, ts, entry_price, entry_costs, executable_price, meta)"
            " VALUES (?,?,?,?,?,?,?)",
            (rule_id, subject_ref, ts, entry_price, entry_costs, executable_price, json.dumps(meta)))
        await self.db.commit()
        return cur.lastrowid

    async def shadow_fills(self, *, rule_id: int | None = None, since: float = 0.0) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM shadow_fills WHERE ts >= ?", [since]
        if rule_id is not None:
            sql += " AND rule_id = ?"
            args.append(rule_id)
        async with self.db.execute(sql + " ORDER BY ts, id", args) as cur:
            rows = await cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["meta"] = json.loads(d["meta"] or "{}")
            out.append(d)
        return out

    async def mark_shadow_fill(self, fill_id: int, mark_price: float | None, mark_ts: float, mark_source: str,
                               outcome: str) -> None:
        await self.db.execute("UPDATE shadow_fills SET mark_price=?, mark_ts=?, mark_source=?, outcome=? WHERE id=?",
                              (mark_price, mark_ts, mark_source, outcome, fill_id))
        await self.db.commit()

    async def latest_mark(self, purchase_id: int) -> tuple[float, float | None, str | None] | None:
        """(ts, net exit, confidence) of the newest mark of a purchase."""
        async with self.db.execute("SELECT ts, net_exit_ton, confidence FROM marks WHERE purchase_id=? "
                                   "ORDER BY ts DESC LIMIT 1", (purchase_id,)) as cur:
            row = await cur.fetchone()
        return (row[0], row[1], row[2]) if row else None

    async def is_paused(self) -> bool:
        return await self.get_setting("paused", "0") == "1"

    async def set_paused(self, paused: bool) -> None:
        await self.set_setting("paused", "1" if paused else "0")

    async def get_dry(self) -> bool:
        """Runtime dry toggle from the bot (/dry). Only matters when .env DRY_RUN=false.
        Fails closed: a missing key means practice."""
        return await self.get_setting("dry", "1") != "0"

    async def set_dry(self, dry: bool) -> None:
        await self.set_setting("dry", "1" if dry else "0")
