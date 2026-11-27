# X / Twitter digest — feasibility (checked 2026-11-27)

## Blocker, stated plainly

**There is no free way to read X timelines.** No X/Twitter connector
exists in Muse, and `social.search` covers Instagram, Threads and Facebook
only — not X.

The official X API v2 moved to pay-per-use in Feb 2026. There is no free
read tier (the old Free tier was write-only, ~500 posts/month, 0 reads,
and is gone from the current pricing page). Current prices, from X's
developer pricing pages via web search 2026-11-27:

| Operation | Price |
|---|---|
| Post read | $0.005 each |
| User read (lookup) | $0.010 each |
| Owned-account read (your own data) | $0.001 each |
| Monthly cap | 3M post reads, then Enterprise |

New paying accounts get a one-time credit (up to ~$70, expires in 3
months). Re-reading the same post on the same UTC day is not re-billed.

Cost at Jordan's likely volume — 10 accounts × 10 posts/day:
~100 post reads + ~10 user reads per day ≈ **$0.60/day ≈ $18/month**.
Cheap, but not free, and it needs a developer account + saved card.

## What was built: `x_digest.py`

A minimal module on the official API, ready to run once Jordan sets it up:

1. `developer.x.com` → create a Project/App → Bearer Token.
2. `export X_BEARER_TOKEN="<token>"` (never in config).
3. Add usernames to `x_config.json`.
4. `python3 x_digest.py --check` — verifies token + config, prints the
   estimated cost per run, spends zero reads.

Per run it resolves each account to a user ID (cached in `x-state.json`),
fetches the timeline (`exclude=replies`, reposts kept and labelled),
dedupes by tweet id, and writes one note per post in the same format as
the other modules. Ratings are transparent heuristics (keyword match to
Jordan's interests + engagement), labelled `rating_basis: heuristic`.

Test status: `--check` verified (reports missing token/accounts, exit 1);
note-writing + dedupe verified with a 2-tweet fixture (1 post + 1 repost,
rerun found 0 new). **No live API call has been made** — that awaits
Jordan's token.

## Routes considered and rejected

- **twscrape (MIT, open source):** reads timelines via a logged-in session
  against X's internal API. Not built: it is session automation against
  X's ToS, breaks whenever X changes its web client, and needs stored
  login cookies. Mentioned here so the option is on the record, not as a
  recommendation.
- **browser.search:** topic search only — no per-account timelines, no
  reposts, no dedup. Not a digest route.
- **Threads connector:** exists and works (read profiles, posts, search,
  trends), but Threads is a different network from X. Could be a
  substitute module later if Jordan wants it; not built here.

## What Jordan needs to do

To switch X on: create the developer app + token (10 minutes), add the
account list to `x_config.json`, `export X_BEARER_TOKEN=...` on the
machine that runs the cron. Until then `run_all.sh` skips X and says so
in the log.
