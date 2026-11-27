# Instagram adapter: command contract

The adapter (`daily_pulse/adapters/instagram.py`) runs a command you provide and reads JSON
from its stdout. It never logs in or scrapes itself.

## Configuration

- Environment variable `IG_CLI_CMD` (name set by `[sources.instagram] cli_cmd_env`): the command,
  split shell-style. Unset or empty: the module reports `deferred: no CLI configured`.
- `[sources.instagram] args = [...]` (optional): extra arguments appended to the command.
- `[sources.instagram] kinds = ["saved", "liked"]`: which kinds to keep, in priority order.

## Output

The command must print a JSON list to stdout and exit 0:

```json
[
  {"shortcode": "Cx1", "url": "https://www.instagram.com/reel/Cx1/", "author": "someone",
   "caption": "the reel's caption", "timestamp": "2026-01-14T10:00:00Z", "source": "saved"}
]
```

- `shortcode`, `url` (strings, required), `source` (`"saved"` or `"liked"`, required).
- `author`, `caption`, `timestamp` (strings, optional).

## Behaviour

- Saved items come before liked items; order within a kind is kept. A shortcode seen twice is
  kept once (first occurrence after sorting, so saved wins).
- Invalid JSON, a non-list, or an invalid entry is logged ("malformed") and skipped; good entries
  still flow. A non-zero exit, timeout or missing command fails this module only.
- Metadata and caption only, no transcription. The run result keeps a ~25 word caption excerpt;
  the full caption is held in `InstagramAdapter.captions` for the summarizer to rate against and
  is never written to notes, the digest or the site.
- With `--fixtures`, `<fixtures>/instagram/items.json` (same format) is read instead of running
  the command.

## Using the Meta connector: `scripts/ig-export.py`

If the owner's Instagram account is linked through the Meta connector CLI (`instagram-cli`), this
script is a ready-made command for the adapter. It lists `saved-posts` then `recently-liked-posts`
(newest first, reels only) and prints the JSON above. It never requests the connector's AI "media
understanding" narratives, only metadata and the caption.

```
export IG_CLI_CMD="python3 /path/to/daily-pulse/scripts/ig-export.py"
export IG_ACCOUNT_ID=...        # from `instagram-cli accounts`; keep it out of the repo
export INSTAGRAM_CLI=...        # optional: the connector's path (default: instagram-cli on PATH)
export IG_EXPORT_LIMIT=20       # optional: most items per kind
```

"Liked" is the connector's *recently* liked list only (no full history). Errors name the failing
step but never print the account id.
