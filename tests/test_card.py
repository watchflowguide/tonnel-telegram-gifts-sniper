import re

from helpers import NOW, L

from tonnel_autobuy.callbacks import DealCB, DigestCB, GateCB, LiveCB, NearCB, PresetCB, RuleCB, ShadowCB
from tonnel_autobuy.notifier import DealView, keyboard_callbacks, keyboard_labels, render_deal_card, render_near_miss
from tonnel_autobuy.peers import PeerQuote, PeerView, TgValue
from tonnel_autobuy.pricing import FeeModel, FloorEstimate, assess, too_good_flag
from tonnel_autobuy.rules import BuyRule, NearMiss
from tonnel_autobuy.wizard import HuntCB


def view(listing=None, *, mode="confirm", pending=True, practice=True, peers=None, floor=1.6, **kw):
    listing = listing or L("814410", 1.0, number=4410, model="Albino (1.2%)", backdrop="Onyx Black (1%)")
    flags = [f for f in [too_good_flag(listing.price_ton, floor)] if f]
    deal = assess(listing, FloorEstimate(floor, "cluster", 4, "high"), FeeModel(10, 0, 0), flags)
    return DealView(listing=listing, deal=deal, rule=BuyRule("Lunar Snake", 3, id=1), mode=mode,
                    status="Waiting for your tap (90 s)", practice=practice, token="AbCdEf12", pending=pending,
                    peers=peers, now=NOW, budget_left=28.9, buys_left=2, **kw)


def test_collection_name_with_lt_is_escaped():
    card = render_deal_card(view(L("x", 1.0, collection="Evil <b>Snake</b> & Co", number=7)))
    assert "Evil &lt;b&gt;Snake&lt;/b&gt; &amp; Co" in card.html
    assert "<b>Snake</b>" not in card.html
    assert "Evil <b>Snake</b> & Co" in card.plain  # plain text keeps the real name


def test_card_contains_cost_floor_confidence_net_profit_break_even():
    card = render_deal_card(view())
    text = card.plain
    assert "You pay 1.10 TON (incl. 10% fee)" in text
    assert "Floor 1.60 (high, 4 comparables)" in text
    assert "If resold at floor: +0.50 TON net (+45%)" in text  # 1.6 net exit (0% sell fee) - 1.10 cost
    assert "break-even 1.45" in text
    assert "Waiting for your tap (90 s)" in text
    assert text.startswith("DEAL Lunar Snake #4410 1.00 TON · floor 1.60 (cluster, 4 comparables, high) · "
                           "you pay 1.10 incl. 10% fee · resold at floor: +0.50 TON net (+45%) · break-even 1.45")
    assert "Albino (1.2%) · Onyx Black (1%)" in text  # Tonnel's rarity suffix from the raw strings
    # contract layout: PRACTICE tag + item header, Risk line, expandable Why
    assert card.html.startswith("<b>PRACTICE</b> · <b>Lunar Snake #4410</b> — 1.00 TON (−38%)")
    assert "<blockquote expandable>Why:" in card.html
    assert re.search(r"^Risk: ", card.plain, re.M)
    assert len(card.html) < 4096


def test_plain_fallback_has_no_tags():
    card = render_deal_card(view(peers=PeerView([PeerQuote("portals", "model", 1.75, 1.75, 1.56, 30)], ["mrkt"],
                                                TgValue("USD", 2.5, 2.5, 2.4, NOW - 3 * 86400, 2.0, 41, False, 1.25))))
    assert "<" not in card.plain and ">" not in card.plain.replace("→", "")
    assert "Elsewhere: Portals 1.75 · MRKT n/a (all-in)" in card.plain
    assert "Telegram: avg ~2.00 TON · last sale ~1.92 TON (3 d ago) · 41 listed" in card.plain


def test_keyboards_by_mode():
    confirm = render_deal_card(view())
    assert keyboard_labels(confirm.keyboard)[:5] == ["Buy 1.10 TON", "Skip", "Mute 24h", "Why?", "Open gift"]
    assert confirm.keyboard.inline_keyboard[0][0].style == "success"
    watch = render_deal_card(view(mode="watch", pending=False))
    assert "Buy 1.10 TON" not in keyboard_labels(watch.keyboard) and "Why?" in keyboard_labels(watch.keyboard)
    urls = [b.url for row in confirm.keyboard.inline_keyboard for b in row if b.url]
    assert urls == ["https://t.me/nft/LunarSnake-4410", "https://t.me/Tonnel_Network_bot"]
    assert keyboard_labels(confirm.keyboard)[-1] == "Open Tonnel"


def test_live_tag_and_best_exit_line():
    card = render_deal_card(view(practice=False, best_exit=("portals", 1.9)))
    assert card.html.startswith("<b>LIVE</b>")
    assert "Best exit: Portals ~1.90 → +0.80 TON net" in card.plain
    own = render_deal_card(view(best_exit=("tonnel", 1.6)))
    assert "Best exit" not in own.plain  # our own market is not "elsewhere"


def test_too_good_badge_and_asking_first_line():
    card = render_deal_card(view(L("t", 0.16, number=4416), asking_first=["price is 10% of floor"]))
    assert "⚠ too good to be true" in card.plain
    assert "Asking first: price is 10% of floor" in card.plain


def test_every_callback_data_fits_64_bytes():
    datas = keyboard_callbacks(render_deal_card(view(llm=True)).keyboard)
    near = render_near_miss(L("n", 2.60), assess(L("n", 2.60), FloorEstimate(3.2, "cluster", 6, "high"),
                                                 FeeModel(10, 0, 0), []),
                            BuyRule("Desk Calendar", 2.75, id=999999), NearMiss("max", 2.86, 2.75, 4.0, 2.90),
                            "AbCdEf12", max_cap=10, practice=True)
    datas += keyboard_callbacks(near.keyboard)
    datas += [DealCB(action="x", token="A" * 8).pack(), NearCB(action="r", token="A" * 8).pack(),
              RuleCB(action="resume", rule_id=10**9, t=2**33).pack(), GateCB(action="resume", t=2**33).pack(),
              LiveCB(nonce="A" * 8).pack(), ShadowCB(action="more", rule_id=10**9, t=2**33).pack(),
              PresetCB(action="ok", name="careful", t=2**33).pack(), HuntCB(action="cg", idx=99).pack(),
              DigestCB(action="keep", rule_id=10**9, cents=10**9, t=2**33).pack()]
    assert datas and all(len(d.encode()) <= 64 for d in datas)


def test_near_miss_raise_button_only_within_max_price():
    deal = assess(L("n", 2.60), FloorEstimate(3.2, "cluster", 6, "high"), FeeModel(10, 0, 0), [])
    miss = NearMiss("max", 2.86, 2.75, 4.0, 2.90)
    shown = render_near_miss(L("n", 2.60), deal, BuyRule("X", 2.75, id=3), miss, "tok12345", max_cap=10, practice=True)
    hidden = render_near_miss(L("n", 2.60), deal, BuyRule("X", 2.75, id=3), miss, "tok12345", max_cap=2.8,
                              practice=True)
    assert "Raise max to 2.90" in keyboard_labels(shown.keyboard)
    assert keyboard_labels(hidden.keyboard) == ["Ignore today", "Why?", "Mute 24h"]
    # the shared alert-card layout: Risk line, expandable Why, Why? and Mute buttons
    assert "Risk: " in shown.plain and "<blockquote expandable>Why: " in shown.html
    assert "It misses rule #3 by 4.0%" in shown.plain
    assert keyboard_labels(shown.keyboard) == ["Raise max to 2.90", "Ignore today", "Why?", "Mute 24h"]
    assert "Missed by a hair: 2.86 TON, your max is 2.75" in shown.plain
