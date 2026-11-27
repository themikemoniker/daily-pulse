# Instagram reels digest — feasibility (verified live, 2026-11-27)

Account tested: `@evolving_vlog` (linked through Meta Accounts Center).
All findings below come from the connector docs plus live CLI calls
(`saved-posts`, `recently-liked-posts`, `media-understanding`), not guesses.

## What works now

| Need | Verdict | Command |
|---|---|---|
| List saved items/reels | YES — full list, paginated, per-collection | `saved-posts [--collection-id]` |
| List liked reels | PARTIAL — "recently-liked" only, no full history | `recently-liked-posts` |
| Fields per item | YES | `media_id`, `media_permalink`, `post_type` (reel/post), `media_type`, `post_caption_text`, `post_creation_time`, `author_info` (username, name, bio, follower_count, verified), `like_count`, `comment_count`, `hashtags` |
| Content summary per reel | YES — stored AI description | `media-understanding --media-ids` → `narrative_summary` + `semantic_understanding` (author breakdown, key quote, related searches) |
| Collections | YES | `saved-collections`, `create-saved-collection`, `save-post` |

The `media-understanding` narrative is genuinely useful: for a saved reel
about Claude Code evals it returned the full 4-point framework (code evals
with code judges, LLM evals with code judges, LLM evals with LLM judges)
with examples. That is the v1 "transcript substitute" — it describes what
the reel shows and says, but it is a stored description, not a transcript,
and an empty field means nothing was stored (calling it again analyzes
nothing new).

## What is blocked

- **No transcript / caption track for reels.** No field carries spoken
  text, and no audio track is exposed. `media_url` is null in list rows.
- **Liked = recent only.** There is no endpoint for the full like history.
- **No per-reel Views / Saves / Shares** in list rows (likes + comments
  only). Per-post insights exist only for the account's OWN posts.
- **Rate limits:** 429 stops all calls for the task; detailed reads
  (`media-understanding`, `post`) are capped at the 50 most recent per run.

## v1 — built: `ig_digest.py`

Metadata + caption excerpt (≤280 chars, full caption at the permalink) +
media-understanding narrative, one note per item in the same format as the
YouTube notes, deduped in `ig-state.json`. Verified live 2026-11-27:
3 saved + 3 liked notes written, rerun found 0 new. Ratings/verdicts are
pending — a `claude -p` summarizer pass (same pattern as YouTube) can fill
them later.

## v2 — transcripts via Whisper (not built; needs verification)

To get real transcripts: download each reel's audio, then transcribe with
faster-whisper locally. The open question is obtaining video bytes:
- `hatch-zeitgeist --raw --post-url <reel URL>` may expose stored media
  locations (documented for post links; unverified for reels).
- `yt-dlp` with Instagram cookies may download reels (unverified, and
  cookie handling for Instagram is more fragile than for YouTube).
- The connector itself offers no download.

Do not build this until one of those routes is verified on a single reel.
Cost if it works: faster-whisper runs locally on the RTX 2060 / Mac, so
transcription is free after the one-time setup.

## What Jordan needs to do

Nothing for v1 — the account is already linked and the module runs.
For v2: nothing until the video-bytes route is verified (that is on us).
