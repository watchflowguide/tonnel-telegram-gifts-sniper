# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-28

First public release.

### Added

- Sniping loop for Tonnel: rules by collection, model, backdrop and symbol, polling the newest and the cheapest listings every 2 s through the public `pageGifts` search, deduplicated by listing id and price. Bundles, auctions and listings without a price are dropped on arrival.
- Telegram login through Telethon to get the Tonnel Mini App `initData`, refreshed every `INIT_DATA_TTL` and on an auth error, with `TONNEL_INIT_DATA` as a manual alternative.
- Honest price math (`pricing.py`): a cluster floor with `high`/`med`/`low` confidence (a floor without a cluster of at least 3 listings is `low`), a fee model (`BUY_FEE_PCT=10` by default, `FEE_PCT` kept as an alias), the all-in cost, the net exit at the floor, the break-even price and match reason codes.
- Trap and lock filter: listings priced in another asset (`TRAP_ASSET`), bundles, auctions and unknown price units are blocked, a future `export_at` counts as a lock (`EXPORT_AT_LOCKS`), and prices under `TOO_GOOD_PCT` of the floor always ask first.
- Deal cards in Telegram HTML with per-rule `auto`, `confirm` and `watch` modes, Buy / Skip / Mute 24h / Why? buttons, a single-listing re-check before a tapped buy, a 90 s expiry and near-miss cards with a one-tap "Raise max".
- Second opinion (`peers.py`, `fx.py`): Telegram's own gift valuation, Portals and MRKT floor snapshot files, optional Portals public floors and a keyless TON to USD rate. Adds `/peers`, `/compare` and `floors --json`.
- Optional Telegram resale market as one more reference (`tg_market.py`, `TG_MARKET=false` by default): rate-limited, cached, paused on FloodWait, and written to `floors_telegram.json`.
- Practice ledger (`ledger.py`): positions marked net of fees, `/pnl`, `/score`, `/scoreboard`, `/sold` and `/export`.
- `/hunt` wizard (`wizard.py`): a rule in at most 6 taps from Tonnel's per-model floors, `t.me/nft` link support and a "would have caught" preview of the last 24 h.
- `/doctor` in the bot and as a CLI command (exit code 1 on any failure).
- Explained decisions: a `decisions` table, `/why` by gift number, listing id or link, a JSON-lines event log, an optional daily digest with spend vs budget and tuning hints, `/mute` and `/unmute` (both accept any spelling of the collection), and an optional "Explain more" button (needs `ANTHROPIC_API_KEY`).
- `TZ_OFFSET_HOURS` (default `0`, UTC) for the times `/why` shows, the digest hour and the end of "Ignore today".
- Shadow rules with recorded fills (`shadow.py`, `shadow_fills` table), a hindsight card, and `/promote`.
- `mock` command that runs the whole pipeline offline with fixtures, with `--tap`, `--crash`, `--panic` (which also shows `/resume` and its confirm step), `--preview`, `--doctor`, `--shadow-hours` and `--telegram`.
- In-place migration of older databases: existing rules and purchases are kept, and rules that came from `rules.json` are adopted instead of duplicated.
- Windows install (README: Self-hosting), a CI workflow (tests on Python 3.11 and 3.12) and 259 offline tests.

### Safety

- `DRY_RUN=true` in `.env` is a hard practice lock that no bot command or stored setting can lift. It also cancels an armed live window and any unused go-live button, so after `DRY_RUN=false` is set again, live mode needs a new `/dry off`.
- Live mode needs `DRY_RUN=false` and `/dry off`. It shows the practice report first, uses a one-time go-live button that expires after 10 minutes and only appears when every rule is `ready`, and switches back to practice after `LIVE_ARM_HOURS`. Practice mode is assumed whenever no valid live window is stored, so a fresh database with `DRY_RUN=false` starts in practice.
- Going live expires every card rendered as PRACTICE, and a tap on such a card can only make a practice buy.
- Every buy, automatic or tapped, practice or live, goes through one locked spend step that re-reads every switch and checks `MAX_PRICE_TON`, `DAILY_BUDGET_TON` and `MAX_BUYS_PER_HOUR` on the all-in cost against fresh totals. Live buys check the balance and re-check the switches and the stored rule (mode, max, filters, a `/preset` minimum discount) once more right before `buyGift`.
- A live buy is recorded as `unknown` before the request leaves. Only an explicit refusal (`{"status":"error"}`, an HTTP 4xx, a rate limit or an auth error) settles it as failed; timeouts, network errors, 5xx errors and any reply other than `{"status":"success"}` stay `unknown` and count toward the limits.
- A floor not backed by a cluster of at least 3 listings has `low` confidence, so two decoy asks cannot pass the default `min_confidence=med`.
- Buy taps expire with their card and are admin-only. Buttons that change state (preset `[Apply]`, digest `[Confirm]`, hindsight cards) expire too. `[Confirm resume]` and the balance breaker's `[Resume]` work once, for 10 minutes, and any later `/pause`, `/panic` or breaker voids them; a crash breaker's rule `[Resume]` only works for the pause it was sent with; the losing-streak breaker voids an unused go-live button. An old button cannot undo a newer `/panic` or breaker.
- On shutdown no new buy starts, and a buy in flight gets its answer and its purchase row before anything is cancelled.
- `rules.json` is parsed strictly: a max price of 0 or below, an unknown key, both spellings of one key, a negative or non-numeric limit and an `enabled` that is not `true`/`false` are config errors, so a typo cannot loosen a rule. Every `/addrule` short key (`max`, `discount`, `profit`, `profit%`, `conf`, `locked`) works there too.
- The `/hunt` wizard rejects a typed max that is not a positive number (`nan`, `inf`, 0).
- `run` holds `data/run.lock`: a second `run` or a `once` refuses to start while it runs, so no second process spends outside the spend lock or opens the Telegram session. CLI `doctor` then checks without the session and points to `/doctor` in the bot.
- `/panic` pauses, forces practice, voids the go-live button and expires pending cards. The flag is stored in the database and survives a restart. `/resume` needs a confirmation.
- Breakers: floor crash per rule, a losing streak, a balance mismatch and repeated failed buys. The loop keeps running its maintenance (card expiry, the live gate, breakers) even while cycles fail.
- `/safety`, plus `/preset` limits that can only tighten.
- New rules, including new `rules.json` entries, start in shadow (practice only) until promoted.
- Secrets are truncated in logs, the session file is set to mode 600, and `/doctor` checks its permissions and 2FA.

### Known limitations

- The live buy path has not been exercised against Tonnel with a real account, and `doctor` has not been run against the live API. Everything marked `TODO(verify)` in the code is listed in the README under "Before going live": the meaning of `export_at`, the buy payload and response, the `wtf` passphrase, the fee schedule, the `filterStats` format, the balance field, the Mini App short name, listing time, anonymous search rate limits, Portals fees, Telegram's "same type" definition, gift slugs and the Telegram resale market fields.
- With the default `EXPORT_AT_LOCKS=true`, a large share of the live market can be skipped as locked: on 2026-10-28, 20 of the 43 Toy Bear listings on the cheapest and newest pages had an `export_at` up to two weeks ahead.
- Fee defaults are assumptions until you set `FEES_VERIFIED=true`.
- A buy already sent to Tonnel cannot be recalled by `/panic`.
- Live buys run one at a time because of the spend lock, and one `run` per data directory is allowed (no lock file on Windows).
- Practice assumes you won every race for the listing. `/scoreboard` counts a race as probably lost only when the listing disappeared while the bot kept polling its collection.
- The first cycle on a fresh database evaluates every listing it fetches; there is no warm-up.
- File rules reset their `mode`, `max` and other fields from `rules.json` on each start. An emptied or deleted `rules.json` leaves the previously loaded rules active.
- The shadow hindsight card takes its main figures from practice positions; the shadow fills add one summary line.
- The first card of a `once` run may lack the USD amount, because the TON rate loads in the background.
