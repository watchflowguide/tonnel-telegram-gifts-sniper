"""Shared event log (schema suite.event.v1): one JSON object per line in data/events.jsonl.

Only the JSON schema and reason-code names are shared with other bots you run; nothing is imported.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger(__name__)

BOT_NAME = "tonnel-autobuy"


class Subject(BaseModel):
    type: Literal["gift", "nft", "token", "market", "wallet"]
    ref: str
    title: str = ""
    url: str | None = None


class Money(BaseModel):
    ccy: Literal["TON", "USDC", "SOL", "ETH"] = "TON"
    price: float | None = None
    ref_price: float | None = None
    fees: float | None = None
    net_edge: float | None = None
    usd: float | None = None


class Reason(BaseModel):
    code: str
    sev: Literal["good", "warn", "bad"]
    p: dict[str, Any] = Field(default_factory=dict)


class Event(BaseModel):
    v: Literal[1]
    ts: float
    bot: str
    kind: Literal["alert", "buy", "skip", "sell", "error", "pause", "resume"]
    mode: Literal["dry", "live", "shadow"]
    id: str
    subject: Subject
    money: Money | None = None
    verdict: Literal["BUY", "WATCH", "AVOID", "SKIP", "COPY"] | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    confidence: Literal["high", "med", "low"] | None = None
    reasons: list[Reason] = Field(default_factory=list)
    rule_id: int | None = None
    target: str | None = None
    tx: str | None = None


def validate_event(data: Any) -> list[str]:
    """Dependency-light validator: returns a list of problems (empty = valid)."""
    if not isinstance(data, dict):
        return ["event must be an object"]
    errors = [f"missing {k}" for k in ("v", "bot", "kind") if k not in data]
    try:
        Event.model_validate(data)
    except ValidationError as exc:
        errors += [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return errors


class EventLog:
    """Append-only JSONL with size-based rotation to <file>.1."""

    def __init__(self, path: str | Path, max_mb: float = 20.0) -> None:
        self.path = Path(path)
        self.max_bytes = int(max_mb * 1024 * 1024)

    def append(self, event: Event | dict[str, Any]) -> None:
        data = event.model_dump(exclude_none=True) if isinstance(event, Event) else event
        line = json.dumps(data, ensure_ascii=False, default=str) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed(len(line.encode()))
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:  # never break the sniping loop for a log line
            log.warning("cannot write event log %s: %s", self.path, exc)

    def _rotate_if_needed(self, incoming: int) -> None:
        if self.path.exists() and self.path.stat().st_size + incoming > self.max_bytes:
            os.replace(self.path, self.path.with_name(self.path.name + ".1"))

    def read(self, limit: int | None = None) -> list[dict[str, Any]]:
        return read_events(self.path, limit=limit)

    def trail(self, ref: str) -> list[dict[str, Any]]:
        return [e for e in self.read() if e.get("subject", {}).get("ref") == ref or e.get("id") == ref]


def read_events(path: str | Path, *, limit: int | None = None, strict: bool = False) -> list[dict[str, Any]]:
    """Read a JSONL event file. Missing file -> []. Bad lines are skipped (or raise with strict)."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    with p.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                if strict:
                    raise
                continue
            if strict and validate_event(data):
                raise ValueError(f"invalid event in {p}")
            out.append(data)
    return out[-limit:] if limit else out
