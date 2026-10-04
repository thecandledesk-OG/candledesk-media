# Breaking News Engine

An add-on to The Candle Desk's daily automation. Every hour it looks for crypto news, checks the sources, sorts each story into a level and decides whether it deserves its own Short. **The 8:15 AM news and 6:15 PM lesson tasks are untouched; this runs beside them.**

## How a story moves through it

1. **Discover.** The hourly task searches the news, opens the pages and writes each story as a candidate: headline, summary, coins, event type, proposed level, confidence, reason, and sources (with whether each page was opened and confirms the claim).
2. **`engine.py ingest`** applies the rules below and stores or updates the story record.
3. **`engine.py plan`** decides what happens next and logs why.
4. For each Short it plans, the task writes a 5-part script, runs `script_tools.py lint`, and (only when not in dry run) voices, renders, hosts and sends it to Buffer.

## Levels

| Level | Examples | What happens |
|---|---|---|
| NORMAL | routine moves, small announcements | saved as a lead for the next morning episode |
| IMPORTANT | confirmed upgrades, big partnerships, ETF or regulatory developments, big exchange news, unusual moves | verified, then a 30–60 s Short the same day |
| BREAKING | ETF approval/rejection, major rulings, big hacks, network outages | verified first, then a Short as fast as possible |

The AI proposes a level and the code checks it:

- **Price moves:** IMPORTANT/BREAKING thresholds per coin, set in `MARKET_MOVE_THRESHOLDS_PCT`.
- **Hacks:** $10M = IMPORTANT, $100M = BREAKING.
- **Outages:** 30 minutes or more on a major network = BREAKING.
- **Not happened yet:** an expected event (like a scheduled upgrade) stays NORMAL until it actually happens.
- **Old news:** a story first reported more than 12 hours ago goes to the daily show.

## Verification

- An IMPORTANT or BREAKING Short needs **2 independent credible sources** that were opened and confirm the claim.
- **Credible** means a primary/official source (`PRIMARY_DOMAINS`, any `.gov`) or a known outlet (`CREDIBLE_SECONDARY_DOMAINS`).
- At least one source must come from those known lists.
- **Social media never counts:** X, Reddit, Telegram, Discord, YouTube, TikTok, Medium, Substack. It can start an investigation, nothing more.
- **Syndicated copies count once:** a Reuters story on Yahoo counts as Reuters.
- **New developments are verified on their own sources,** not on older reports about the same event.
- **If verification fails,** the story is marked `UNVERIFIED` with all URLs saved and re-checked every hour. After 6 hours it is flagged for human review.

## Duplicates

A new report is matched to an existing event when any of these hold:

- the AI links it explicitly;
- it shares a source URL with the event;
- it names the same specific identifier (e.g. `PermissionDelegationV1_1`, `BatchV1_1`, `EIP-7702`) about the same coin;
- its headline is similar enough to the event's headline.

When a report matches, its sources are merged into the event; it does not become a new video.

A genuinely new development can still earn another Short:

- when it raises the level (NORMAL → IMPORTANT, for example); or
- when it is a new BREAKING turn on a story already covered.

Stories the morning episode already covered are recorded once a day (`covered-in-daily`), so an extra Short won't repeat them.

## Priority and limits (`config.json`)

| Setting | Default | Meaning |
|---|---|---|
| `MAX_IMPORTANT_SHORTS_PER_DAY` | 3 | extra IMPORTANT Shorts beyond that go to the daily show |
| `MAX_BREAKING_SHORTS_PER_DAY` | 3 | beyond that, only stories flagged `emergency` (logged with the reason) |
| `HARD_MAX_EXTRA_SHORTS_PER_DAY` | 6 | absolute ceiling |
| `MIN_MINUTES_BETWEEN_EXTRA_POSTS` | 60 | IMPORTANT waits; BREAKING may skip it (logged) |
| `QUIET_MINUTES_AROUND_SLOTS` | 30 | IMPORTANT Shorts stay clear of 8:15 AM and 6:15 PM; BREAKING doesn't wait |
| `IMPORTANT_MAX_DELAY_HOURS` | 6 | an IMPORTANT story that can't get a slot goes to the daily show |

Order: BREAKING → IMPORTANT → the scheduled daily content → NORMAL leads for future episodes.

## Safety switches

| Setting | Now | Effect |
|---|---|---|
| `BREAKING_NEWS_ENABLED` | true | master switch |
| `DRY_RUN` | **true** | detect, verify, classify and script, log what it *would* publish; render and post nothing |
| `AUTO_PUBLISH_LEVEL_2` | **false** | when off (and not in dry run), IMPORTANT Shorts are rendered and saved as **Buffer drafts** for approval |
| `AUTO_PUBLISH_LEVEL_3` | **false** | same for BREAKING |
| `MIN_CONFIDENCE_FOR_AUTO` | 0.75 | below this, even auto mode only makes a draft |

To go live in stages:

1. Set `DRY_RUN` to false. Shorts then arrive as Buffer drafts that you approve.
2. Later, turn on the auto-publish switches.

## Files

- `config.json`: every setting above.
- `engine.py`: records, rules, limits, gates and the audit log. Run it with no arguments for the command list.
- `script_tools.py`:
  - lints breaking scripts (structure, 30–60 s length, hype phrases, predictions, digits, captions);
  - builds a *local* breaking copy of the news player, with a `BREAKING` label and a sources ticker. The live news player is never edited.
- `state/stories.json`: one record per event, with every field in the spec.
- `state/log.jsonl`: the audit trail. `engine.py report` prints it readably.
- `tests/`: `python3 breaking/tests/test_engine.py`. Covers BTC, ETH, XRP and SOL cases.

Breaking videos are hosted on their own branch, `breaking-media`, so they never collide with the daily `media` drops.
