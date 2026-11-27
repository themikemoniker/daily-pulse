# Seed comparison: the Muse digest scripts vs this repo

Reference: `seed/muse-scripts/` on branch `seed/muse-scripts` (`yt_digest.py`, `ig_digest.py`, `x_digest.py`,
`gh_digest.py`, `digest_all.py`, `run_all.sh`, `run.sh`, prompts and configs). Repo: `main` at the time of
writing, with every build ticket merged and the first live runs behind it. Verdicts are per module:
KEEP-REPO, KEEP-SCRIPT or MERGE. The scripts were only read, never run.

## Verdicts

| Module | Script | Repo | Verdict | Reason |
|---|---|---|---|---|
| YouTube: playlist listing | `yt_digest.py` | `adapters/youtube.py` | KEEP-REPO | Carries duration and views into the quality gate, filters private and deleted entries, detects order, prunes state. |
| YouTube: metadata | `fetch_metadata` | `_fetch` | KEEP-REPO | Typed errors, a client seam, fixtures. The script only adds `chapters` (used for frames). |
| YouTube: captions and VTT cleaning | `fetch_captions`, `parse_json3_file` | `clean_vtt`, `subtitles` | KEEP-REPO | The repo's cleaner dedupes auto-caption rolls and drops sound-tag-only tracks. The json3 fallback is not worth the code. |
| YouTube: 429, bot check, failures | log and retry next run | `YtDlpRateLimited`, backoff, pacing, strike counters | KEEP-REPO | The script marks a failed caption fetch as fetched and never retries it. |
| YouTube: state and idempotency | `save_json` | `atomic_write_json`, per-video counters | KEEP-REPO | The script's writes are not atomic. |
| YouTube: quality filtering | none | `quality.py` | KEEP-REPO | The script has no duration, channel or triage rules. This is what weeds out low-quality playlist items. |
| Instagram adapter | `ig_digest.py` | `adapters/instagram.py` | MERGE | Repo wins on structure and safety. Port the caption-first-line title, a clear missing-binary error, a `limit`, and likes, comments and hashtags as metrics. |
| X adapter | `x_digest.py` | `adapters/x.py` (stub) | MERGE | Port as an opt-in adapter that never spends unless approved (below). |
| GitHub: rising candidates | `rising_feed` | `_rising` | MERGE | The script searches per topic and builds a pool of fast growers plus topic matches. The repo takes the top 10 by stars of everything, which is mostly games and emulators. |
| GitHub: relevance rating | `rate_repos_batch`, `prompts/rate-repos.md` | per-item `claude -p` with the shared rubric | MERGE | Rating itself is already relevance-first in the repo (see the bug note). The script batches a whole pool into one call, which the repo's 30-call budget needs. |
| GitHub: diamond rule | `is_diamond` (relevance 7+ and 50+ stars/day) | `digest.is_diamond` (rating 9+ and under 48h) | MERGE | Both are kept: the owner's GitHub rule becomes an additional path for GitHub items. |
| GitHub: README excerpt | `fetch_readme_excerpt` | none | MERGE | Opt-in, token only, never stored in a note. |
| Digest builder | `digest_all.py` | `digest.py` | KEEP-REPO | Escaped markdown, scheme-checked links, email-safe HTML, deterministic and atomic. The script groups by source and links local paths. |
| Static site | none | `site.py` | KEEP-REPO | No script counterpart. |
| Summarizer prompt and rubric | `prompts/summarize.md` | `prompts/summarize.md`, `rubric.md` | MERGE | The repo's JSON contract and untrusted-text rules are better. Port five failure-mode sentences (below), without the personal names. |
| Runner and cron wrapper | `run_all.sh`, `run.sh` | `scripts/run-daily.sh`, `runner.py` | KEEP-REPO | Lock, stale-lock alert, notifier, per-source isolation. The script only logs a warning and continues. |
| Config shape | `*_config.json` | `config/daily-pulse.toml` | KEEP-REPO | One TOML file. Only a few X and Instagram knobs are borrowed. |

## The star-velocity bug: the repo does not have it

The script's first version rated repos by star velocity and gave 10/10 to a game-studio agent and a typing
cat. The repo's `rubric.md` already says "do not score generic quality, polish or popularity". Checked on real
data: the fourth live run rated ten rising repos (including a game, an emulator and a fork of a game) 1 to 3,
all `skip`. What the repo lacks is the script's candidate selection (topic searches), batching, and the owner's
diamond rule. A one-line "velocity is a signal, never the score" is added to the prompt anyway.

## What was ported, and what was deliberately changed

- `prompts/rate-repos.md`: ported with the text unchanged except that the person's full name and the client
  contact's name were replaced by neutral wording ("the reader", "the client"), because this repo is public.
- GitHub relevance logic (`topic_score`, `velocity_of`, `is_diamond`, `format_repo_for_rating`,
  `parse_batch_ratings`, `rate_repos_batch`, the rating pool): ported into `daily_pulse/repo_rating.py` and the
  GitHub adapter. Adapted: the summarizer call goes through the repo's tool-less `claude -p` wrapper so the
  per-run call cap and the timeout apply. With no summarizer an item is left unrated and retried, never given a
  guessed score.
- Prompt wording: say so when a video is too basic or unverified hype, never invent numbers or timestamps,
  say what would confirm a diamond, velocity is never the score.
- Instagram: caption-first-line title, a clear error when the configured command is missing, a `limit`,
  likes, comments and hashtags as metrics.
- X: opt-in adapter, off unless `paid_api_approved = true` and the token variable is set, fixtures-only tests.

## Not ported, and why

- Frames with ffmpeg (`grab_frames`): needs extra binaries, and frames are a v2 item.
- `--cookies` support and `--no-check-certificate`: cookies are a credential and the repo deliberately has no
  login path; disabling TLS verification is never acceptable.
- Instagram media-understanding narratives and author bios: a derived summary of the video stored on disk is a
  policy call for the owner. Not ported until decided.
- The script's note writers: they store a 280-character caption excerpt, tweet text, author bios and local file
  paths. The repo's notes keep own-word summaries and short quotes only.
- Script transcripts written with default permissions and linked from notes.
- Hard-coded personal paths, the personal handle and account id, and the personal names in the prompts.
- The keyword heuristic as a fallback rating: an item with no summarizer is unrated, not scored.
- `json3` caption fallback and the `parse_rating_reason` fallbacks for malformed notes: the repo is stricter on
  purpose.

## Still genuinely new work

- Installing the crontab (the repo ships the wrapper and a line to paste; nothing is installed).
- A decision on Instagram media-understanding narratives.
- X credentials and spend approval (about $0.60 a day at 10 accounts by the script's own notes, not verified).
  A mapping from the seed display names to usernames is also needed.
- A `GITHUB_TOKEN` on deux: 60 unauthenticated requests an hour is too few for a day's lookups plus reruns.
