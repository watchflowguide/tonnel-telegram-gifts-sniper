import asyncio
import re
from pathlib import Path

from helpers import LS, NOW, L, demo_engine, settings

from tonnel_autobuy import explain
from tonnel_autobuy.config import Settings
from tonnel_autobuy.digest import aggregate_events, build_digest, load_peer_events
from tonnel_autobuy.events import BOT_NAME, EventLog, read_events, validate_event
from tonnel_autobuy.notifier import DealView, render_deal_card
from tonnel_autobuy.pricing import FeeModel, FloorEstimate, assess
from tonnel_autobuy.rules import REASON_CODES, BuyRule

PKG = Path(__file__).resolve().parents[1] / "tonnel_autobuy"
FIXTURE = Path(__file__).parent / "fixtures" / "events_sample.jsonl"


def _codes_in_source() -> set[str]:
    """Reason codes as they appear in the code: report.reasons[...], _decide(...), downgrades, limits, taps."""
    pattern = re.compile(r'"([A-Z][A-Z]+(?:_[A-Z]+)+|[A-Z]{4,})"')
    settings_keys = set(Settings.model_fields)
    ignore = {"TON", "USD", "USDC", "USDT", "JPY", "KRW", "VND", "CLP", "ISK", "UGX", "XAF", "XOF", "BHD", "KWD",
              "OMR", "JOD", "TND", "IQD", "LYD", "MRKT", "HTML", "STATUS", "SKIP", "WATCH", "AVOID", "BUY", "COPY",
              "TRAP", "LIVE", "PRACTICE", "PAUSED", "MATCH", "BOUGHT"}
    found: set[str] = set()
    for name in ("engine.py", "peers.py", "pricing.py", "rules.py"):
        for code in pattern.findall((PKG / name).read_text()):
            if code not in settings_keys and code not in ignore:
                found.add(code)
    return found


def test_every_reason_code_has_an_english_template():
    codes = set(REASON_CODES) | set(explain.SUITE_CODE) | set(explain.SUITE_CODE.values()) | _codes_in_source()
    codes |= {"PAUSED"}
    missing = sorted(c for c in codes if c not in explain.REASON_TEXT)
    assert missing == []
    assert all(isinstance(t, str) and t and t.isascii() for t in explain.REASON_TEXT.values())


def test_validator_rejects_missing_v_bot_kind():
    good = {"v": 1, "ts": NOW, "bot": BOT_NAME, "kind": "skip", "mode": "dry", "id": "x",
            "subject": {"type": "gift", "ref": "x"}, "reasons": []}
    assert BOT_NAME == "tonnel-autobuy" and validate_event(good) == []
    for key in ("v", "bot", "kind"):
        bad = dict(good)
        bad.pop(key)
        assert any(key in e for e in validate_event(bad))
    assert validate_event({**good, "kind": "dance"})
    assert validate_event("nope")


def test_rotation_at_events_max_mb(tmp_path):
    log = EventLog(tmp_path / "events.jsonl", max_mb=0.001)  # ~1 KB
    for i in range(30):
        log.append({"v": 1, "ts": NOW + i, "bot": BOT_NAME, "kind": "skip", "mode": "dry", "id": str(i),
                    "subject": {"type": "gift", "ref": str(i)}, "reasons": []})
    assert (tmp_path / "events.jsonl.1").exists()
    assert (tmp_path / "events.jsonl").stat().st_size <= 1100


def test_rendered_alert_is_short_and_escaped():
    listing = L("x", 1.0, collection="<script>" + "A" * 300, number=1)
    deal = assess(listing, FloorEstimate(1.6, "cluster", 4, "high"), FeeModel(10, 0, 0), [])
    card = render_deal_card(DealView(listing, deal, BuyRule(listing.collection, 3, id=1), "confirm", "Waiting",
                                     True, token="tok12345", pending=True))
    assert len(card.html) < 4096 and "<script>" not in card.html and "&lt;script&gt;" in card.html


def test_llm_path_makes_no_call_without_key():
    async def boom(*args):
        raise AssertionError("network call attempted")

    s = settings(ANTHROPIC_API_KEY="")
    assert asyncio.run(explain.explain_more({"x": 1}, s, call=boom)) is None
    assert not explain.llm_available(s)

    seen = []

    async def fake(key, model, prompt):
        seen.append((key, model, prompt))
        return "Plain words."

    keyed = settings(ANTHROPIC_API_KEY="sk-secret-123")
    assert asyncio.run(explain.explain_more({"title": "Lunar Snake #1"}, keyed, call=fake)) == "Plain words."
    key, model, prompt = seen[0]
    assert model == "claude-sonnet-5-5" and "sk-secret-123" not in prompt and "Lunar Snake #1" in prompt


def test_digest_aggregation_matches_fixture_events():
    events = read_events(FIXTURE)
    assert len(events) == 7  # the garbage line is skipped
    agg = aggregate_events(events, 1800000000, 1800086400)
    assert (agg["alerts"], agg["buys"], agg["skips"]) == (1, 2, 3)
    assert agg["buys_by_mode"] == {"dry": 1, "live": 1}
    assert agg["usd"] == 6.25
    assert agg["top_reasons"] == [("PRICE_ABOVE_MAX", 2), ("FEES_EAT_PROFIT", 1)]


def test_peer_event_files_missing_or_malformed_are_skipped(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"hello": 1}\n')
    out = load_peer_events([str(tmp_path / "missing.jsonl"), str(bad), str(FIXTURE)])
    assert list(out) == [str(FIXTURE)]


def test_engine_writes_valid_events_and_digest_has_top_reasons(tmp_path):
    async def go():
        log = EventLog(tmp_path / "events.jsonl")
        engine, storage, client, notifier, clock = await demo_engine(events=log)
        try:
            await engine.run_cycle()
            events = log.read()
            assert events and all(validate_event(e) == [] for e in events)
            assert {e["bot"] for e in events} == {"tonnel-autobuy"}
            kinds = {e["kind"] for e in events}
            assert {"alert", "buy", "skip"} <= kinds
            f2 = [e for e in events if e["subject"]["ref"] == LS(4412)]
            assert f2[0]["reasons"][0]["code"] == "FEES_EAT_PROFIT"
            trap = [e for e in events if e["subject"]["ref"] == "830054"]
            assert trap[0]["verdict"] == "AVOID"
            text = await build_digest(engine, clock() - 86400, clock() + 1)
            assert "Top skip reasons: OVER_MAX" in text
        finally:
            await storage.close()

    asyncio.run(go())


def test_digest_suite_section_reads_sibling_events(tmp_path):
    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(PEER_EVENT_FILES=str(FIXTURE)))
        try:
            text = await build_digest(engine, 1800000000, 1800086400)
            assert "Your other bots:" in text and "mrkt-autobuy: 1 alert · 2 buys · 3 skips · $6.25 bought" in text
        finally:
            await storage.close()

    asyncio.run(go())


def test_digest_spend_line_and_confirmed_raise_hint():
    from tonnel_autobuy.bot import BotContext, digest_action
    from tonnel_autobuy.digest import digest_card, hint_keyboard

    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(MAX_PRICE_TON=10))
        try:
            await engine.run_cycle()
            text, hint = await digest_card(engine, clock() - 86400, clock() + 1)
            assert "Spend vs budget: practice 4.40 / 50 TON (9%) · live 0.00 / 50 TON (0%)" in text
            assert "would be +net today" in text
            assert hint is not None and hint.rule_id == 3 and (hint.old_max, hint.new_max) == (2.75, 2.9)
            from tonnel_autobuy.notifier import keyboard_labels

            assert keyboard_labels(hint_keyboard(hint)) == ["Raise to 2.90", "Keep"]
            ctx = BotContext(engine.settings, storage, engine, client)
            ask, kb = await digest_action(ctx, "ask", 3, 290)
            assert "Raise rule #3 max 2.75 → 2.90 TON?" in ask and kb is not None
            assert (await storage.get_rule(3)).max_price_ton == 2.75  # asking changes nothing
            keep, _ = await digest_action(ctx, "keep", 3, 290)
            assert "kept" in keep and (await storage.get_rule(3)).max_price_ton == 2.75
            sent = int(clock())  # the [Confirm] button's own time (DigestCB.t)
            over, _ = await digest_action(ctx, "ok", 3, 1100, sent)
            assert "MAX_PRICE_TON 10" in over and (await storage.get_rule(3)).max_price_ton == 2.75
            # A [Confirm] older than BUTTON_TTL_SEC (or one without a time) changes nothing.
            clock.advance(601)
            stale, _ = await digest_action(ctx, "ok", 3, 290, sent)
            assert "out of date" in stale and (await storage.get_rule(3)).max_price_ton == 2.75
            assert "out of date" in (await digest_action(ctx, "ok", 3, 290))[0]
            ok, _ = await digest_action(ctx, "ok", 3, 290, int(clock()))
            assert "max raised 2.75 → 2.90" in ok and (await storage.get_rule(3)).max_price_ton == 2.9
        finally:
            await storage.close()

    asyncio.run(go())


def test_digest_never_offers_to_raise_an_env_limit():
    from tonnel_autobuy.digest import digest_card

    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(MAX_PRICE_TON=2.8))
        try:
            await engine.run_cycle()
            text, hint = await digest_card(engine, clock() - 86400, clock() + 1)
            assert hint is None and "would pass MAX_PRICE_TON 2.8" in text
        finally:
            await storage.close()

    asyncio.run(go())
