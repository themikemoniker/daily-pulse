"""The watchlist: public creator handles to follow, kept in `config/watchlist.toml`.

`daily-pulse watch add|list|remove` edit it. The file is machine-managed (stdlib `tomllib` reads
TOML but cannot write it), so it is regenerated whole and atomically. It holds public handles only.
`load_config` merges it into the source tables, so the main config keeps working unchanged.
"""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from urllib.parse import urlparse

SOURCES = ("youtube", "github", "x", "instagram")
# (watchlist table, key) -> the `[sources.<table>]` key it is merged into
MERGE = {
    ("github", "accounts"): "watch_accounts",
    ("github", "repos"): "watch_repos",
    ("youtube", "channels"): "channels",
    ("x", "usernames"): "usernames",
    ("instagram", "accounts"): "accounts",
}
INERT = {"x": "paid and off until paid_api_approved = true", "instagram": "needs a configured Instagram CLI"}
HEADER = ("# Creators to watch. Managed by `daily-pulse watch add|list|remove`: edits by hand are kept\n"
          "# only if they stay plain lists of strings. Public handles only; nothing private goes here.\n")

_GH_USER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_GH_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_X_USER = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_IG_USER = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_YT_HANDLE = re.compile(r"^@[A-Za-z0-9._-]{3,60}$")
_YT_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_YT_NAME = re.compile(r"^[A-Za-z0-9._%-]{1,100}$")
_HOSTS = {"youtube.com": "youtube", "www.youtube.com": "youtube", "m.youtube.com": "youtube",
          "github.com": "github", "www.github.com": "github",
          "x.com": "x", "www.x.com": "x", "twitter.com": "x", "www.twitter.com": "x",
          "instagram.com": "instagram", "www.instagram.com": "instagram"}


class WatchError(ValueError):
    """The input cannot be turned into a watchlist entry; the message says what to do."""


def _youtube(path_parts: list[str]) -> str:
    first = path_parts[0] if path_parts else ""
    if _YT_HANDLE.match(first):
        return f"https://www.youtube.com/{first}"
    if first == "channel" and len(path_parts) > 1 and _YT_CHANNEL_ID.match(path_parts[1]):
        return f"https://www.youtube.com/channel/{path_parts[1]}"
    if first in ("c", "user") and len(path_parts) > 1 and _YT_NAME.match(path_parts[1]):
        return f"https://www.youtube.com/{first}/{path_parts[1]}"
    if first in ("watch", "shorts", "playlist", "live", "embed") or not first:
        raise WatchError("that is a video or playlist link, not a channel: use the channel URL "
                         "(youtube.com/@handle)")
    raise WatchError(f"cannot read a YouTube channel from {'/'.join(path_parts)!r}: "
                     "use youtube.com/@handle or youtube.com/channel/UC...")


def _entry(source: str, name: str) -> tuple[str, str, str]:
    """(source, key, value) for a handle already known to belong to `source`."""
    name = name.strip().strip("/")
    if source == "github":
        if "/" in name:
            owner, repo = name.split("/", 1)
            repo = repo.split("/", 1)[0].removesuffix(".git")
            if not (_GH_USER.match(owner) and _GH_REPO.match(repo)) or repo in (".", ".."):
                raise WatchError(f"{name!r} is not a GitHub owner/repo")
            return "github", "repos", f"{owner}/{repo}"
        if not _GH_USER.match(name):
            raise WatchError(f"{name!r} is not a GitHub username")
        return "github", "accounts", name
    if source == "x":
        name = name.lstrip("@")
        if not _X_USER.match(name):
            raise WatchError(f"{name!r} is not an X handle (letters, digits, underscore; up to 15)")
        return "x", "usernames", name
    if source == "instagram":
        name = name.lstrip("@")
        if not _IG_USER.match(name):
            raise WatchError(f"{name!r} is not an Instagram handle")
        return "instagram", "accounts", name
    if source == "youtube":
        if _YT_CHANNEL_ID.match(name):
            return "youtube", "channels", f"https://www.youtube.com/channel/{name}"
        if _YT_HANDLE.match(name) or (_YT_HANDLE.match("@" + name) and not name.startswith("@")):
            return "youtube", "channels", f"https://www.youtube.com/{name if name.startswith('@') else '@' + name}"
        raise WatchError(f"{name!r} is not a YouTube @handle or channel id (UC...)")
    raise WatchError(f"unknown source {source!r}; use one of {', '.join(SOURCES)}")


def parse_target(text: str, source: str | None = None) -> tuple[str, str, str]:
    """Turn a URL or handle into `(source, key, value)`. Never touches the network."""
    text = (text or "").strip()
    if not text:
        raise WatchError("give a URL or handle")
    if source is not None and source not in SOURCES:
        raise WatchError(f"unknown source {source!r}; use one of {', '.join(SOURCES)}")
    if "://" in text or text.split("/", 1)[0].lower() in _HOSTS:
        parsed = urlparse(text if "://" in text else "https://" + text)
        host = (parsed.hostname or "").lower()
        found = _HOSTS.get(host)
        if found is None:
            raise WatchError(f"cannot tell the platform of {host!r}: use a youtube, github, x or instagram link")
        if source and source != found:
            raise WatchError(f"--source {source} does not match the {found} link")
        parts = [p for p in parsed.path.split("/") if p]
        if found == "youtube":
            return "youtube", "channels", _youtube(parts)
        if not parts:
            raise WatchError(f"no account in {text!r}")
        if found == "github":
            return _entry("github", "/".join(parts[:2]))
        return _entry(found, parts[0])
    if source is None:
        if re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", text):
            return _entry("github", text)
        raise WatchError(f"cannot tell which platform {text!r} is on: add --source "
                         f"({', '.join(SOURCES)}) or paste the profile link")
    return _entry(source, text)


def _empty() -> dict[str, dict[str, list[str]]]:
    return {"github": {"accounts": [], "repos": []}, "youtube": {"channels": []},
            "x": {"usernames": []}, "instagram": {"accounts": []}}


def load(path: Path) -> dict[str, dict[str, list[str]]]:
    """The watchlist as `{source: {key: [values]}}`; a missing file is an empty list."""
    data = _empty()
    path = Path(path)
    if not path.exists():
        return data
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
        raise WatchError(f"{path} is not valid TOML: {exc}") from None
    for source, table in raw.items():
        if source not in data or not isinstance(table, dict):
            raise WatchError(f"{path}: unknown table [{source}]")
        for key, values in table.items():
            if key not in data[source]:
                raise WatchError(f"{path}: unknown key {source}.{key}")
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise WatchError(f"{path}: {source}.{key} must be a list of strings")
            data[source][key] = [v.strip() for v in values if v.strip()]
    return data


def render(data: dict[str, dict[str, list[str]]]) -> str:
    out = [HEADER]
    for source, table in data.items():
        out.append(f"\n[{source}]\n")
        for key, values in table.items():
            body = "".join(f'  "{v}",\n' for v in values)
            out.append(f"{key} = [\n{body}]\n" if values else f"{key} = []\n")
    return "".join(out)


def save(path: Path, data: dict[str, dict[str, list[str]]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(render(data), encoding="utf-8")
    os.replace(tmp, path)


def add(path: Path, source: str, key: str, value: str) -> bool:
    """True if added, False if already watched (compared case-insensitively)."""
    data = load(path)
    values = data[source][key]
    if value.lower() in (v.lower() for v in values):
        return False
    values.append(value)
    save(path, data)
    return True


def remove(path: Path, source: str, key: str, value: str) -> bool:
    data = load(path)
    values = data[source][key]
    kept = [v for v in values if v.lower() != value.lower()]
    if len(kept) == len(values):
        return False
    data[source][key] = kept
    save(path, data)
    return True


def merge_into(sources: dict[str, dict], data: dict[str, dict[str, list[str]]]) -> None:
    """Union the watchlist into the `[sources.*]` tables, keeping order and dropping duplicates."""
    for (source, key), target in MERGE.items():
        extra = data.get(source, {}).get(key, [])
        if not extra:
            continue
        table = sources.setdefault(source, {"enabled": False})  # a source the config never mentions stays off
        current = table.get(target, [])
        if not isinstance(current, list):
            raise WatchError(f"sources.{source}.{target} must be a list to merge the watchlist into")
        seen = {str(v).lower() for v in current}
        table[target] = list(current) + [v for v in extra if not (v.lower() in seen or seen.add(v.lower()))]
