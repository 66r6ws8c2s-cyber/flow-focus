# Flow Focus

Static GitHub Pages dashboard. [`index.html`](index.html) reads [`latest.json`](latest.json) and draws one card per ticker from the current HCPG watchlist (GOOGL, MSFT, AAPL, AMZN, NBIS, AMD, TSLA, SHOP, CRWD, PLTR, AVGO, NVDA, META, WMT, MU, ABNB, SPOT). SPY and QQQ are context on the Market Tide line only.

The page refreshes itself. GitHub Actions runs the generator on a schedule, then commits `latest.json` (and `baselines.json` after the close) back to `main`. No agent, no paid runner minutes on this public repo. The only credential is the `UW_API_KEY` repo secret. It is passed as an environment variable and is never printed, logged, or written into a file.

## Schedule

Cron, in UTC, weekdays, plus **Run workflow** (`workflow_dispatch`):

- `*/15 13-20 * * 1-5` — every 15 minutes from 13:00 through 20:45 UTC
- `0 21 * * 1-5` — 21:00 UTC, so the 4:00 PM Eastern close is covered in winter

That window covers **9:30 AM–4:00 PM America/New_York** in both EST (UTC−5) and EDT (UTC−4):

| | 9:30 AM ET | 4:00 PM ET |
| --- | --- | --- |
| EDT (UTC−4) | 13:30 UTC | 20:00 UTC |
| EST (UTC−5) | 14:30 UTC | 21:00 UTC |

`compute.py` checks the clock in Eastern time before it builds a request. Weekends, NYSE holidays, and anything outside 9:30 AM–4:10 PM ET (10 minutes of grace so a slow 4:00 PM job still runs) exit immediately. Those runs do not call the API and do not commit. Early closes (the day after Thanksgiving, Christmas Eve when it is marked) end at **1:00 PM ET**.

A run that fails to fetch anything leaves the previous `latest.json` in place. Raw prints stay in `data/trades_YYYYMMDD.json`, which is gitignored and reused across jobs with Actions cache. `snapshots.csv` is local-only.

## The call on each card

Every ticker has a `call` string, shown as the first line of the card. It is exactly **Nothing significant** unless both of these are true:

1. Directional lean is 65% or more one way (bullish share ≥ 65, or ≤ 35). Lean is bullish premium divided by bullish + bearish premium, same number as the big % on the card.
2. The $200K+ orders back that side:
   - **one-sided premium** — that side’s $200K+ premium is at least $200K and at least twice the other side, or
   - **a repeat** — the same contract printed at least twice for $200K+ each, all on that side, totaling at least $400K, and that side’s $200K+ premium is still larger than the other side.

Bullish $200K+ orders are calls bought at the ask and puts sold at the bid. Bearish orders are puts bought at the ask and calls sold at the bid. Mid prints, multi-leg / stock-tied prints, and options more than 15% in the money are excluded, same as the rest of the card.

The sentence uses the underlying price already on those prints. Nothing else is treated as a level:

- Bullish, and the last price is down or flat (within +0.15% of the first print) in the bottom 20% of a session range at least 0.2% wide: `Bulls absorbing the selling near <session low>, could flip up`
- Bullish, and price is down 0.3% or more but not at that low: `Whales buying the dip`
- Bearish, and the last price is in the top 20% of that range: `Bears selling into the high near <session high>`
- Bearish, and price is up 0.3% or more but not at that high: `Whales selling the rip`
- Otherwise, when the two tests above pass: `Big money leaning bullish` or `Big money leaning bearish`

`call_dir` is `bull`, `bear`, or null, and is the only extra color on that line. The footer states that the line is rule-based and not trade advice.

Open the layout without a key: `index.html?fixture=fixtures/preview.json` (synthetic prints, not a live session).

## Files

- `compute.py` — one refresh. `from compute import compute`; `compute(save=False, now=...)` does not write.
- `q.py` — Unusual Whales GET. Reads `UW_API_KEY` at request time.
- `holidays.py` — NYSE full-day closures through 2028, plus the early closes. A year past the table raises instead of guessing.
- `baseline.py` / `baselines.json` — 10-day time-of-day averages. The closing run adds today from the feed cache (no API) when the cache reaches the close.
- `index.html` — the page. Polls `latest.json` every 60 seconds.
- `fixtures/preview.json` — synthetic session used to check the cards.
- `sample.html` — frozen 19 Nov 2026 sample of an older layout. It does not refresh.
- `.github/workflows/refresh.yml` — the schedule above.

## Method

Orders of $25K+ premium since 9:30 AM ET (SPY/QQQ: $100K+, tide context only).

- Bullish = calls at the ask + puts at the bid. Bearish = puts at the ask + calls at the bid. Split under $200K vs $200K+.
- Skipped: mid or no-side prints; multi-leg / stock-tied / complex prints by OPRA condition (`upstream_condition_detail` starting with `m` or `t`, plus `cbmo` / `cmpr`, and the `futures_floor` flag); options more than 15% in the money.
- The window is 9:30 AM through min(now, the close). Pulls set `older_than` to now, so a replay does not see later prints.
- `series` is the cumulative lean at each 15-minute mark, from the cache.
- Lean = bull / (bull + bear). Speed compares the last 15 minutes with the earlier session (±10 points picking up or fading; a cross of 55/45 is a flip; under $100K in 15 minutes is quiet).
- Focus (the outlined card) is unchanged: lean ≥ 65% or ≤ 35%, at least $1M of $200K+ premium on that side and more than the other side, and still accelerating. The call line does not require the $1M or the acceleration test.
- Vs normal compares order counts with the previous 10 trading days at this time of day. The readiness line is the same four checks as before (lean, $1M+ of $200K+ dollars and more than the other side, acceleration, count vs normal). That $1M bar is higher than the call line, so a card can have a significant call and still be Thin.

API use when the cache is warm: about one trades call per ticker, extra pages only when there are more than 500 new prints, plus one market-tide call. The first run of a session refetches the day; later runs reuse `data/`.
