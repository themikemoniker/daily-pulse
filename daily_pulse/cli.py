"""Command line entry point."""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import tomllib
from datetime import date as _date
from pathlib import Path

from daily_pulse.adapters.registry import register_builtin
from daily_pulse.config import DEFAULT_CONFIG, ConfigError, load_config
from daily_pulse.digest import parse_time, write_digest
from daily_pulse.lock import LockError, LockHeld, RunLock
from daily_pulse.runner import run_pipeline
from daily_pulse.site import build_site
from daily_pulse.state import StateError, atomic_write_json


def _iso_date(text: str) -> str:
    try:
        return _date.fromisoformat(text).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date {text!r}, expected YYYY-MM-DD") from None


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="daily-pulse")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run the pipeline once")
    r.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    r.add_argument("--date", default=None, type=_iso_date, help="run date, YYYY-MM-DD (default: today)")
    r.add_argument("--out", type=Path, required=True, help="output directory")
    r.add_argument("--fixtures", type=Path, default=None, help="read recorded inputs, no network")
    r.add_argument("--dry-run", action="store_true", help="write no state or output")
    d = sub.add_parser("digest", help="build digests/<date>.md and .html from the day's notes")
    d.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    d.add_argument("--date", default=None, type=_iso_date, help="digest date, YYYY-MM-DD (default: today)")
    d.add_argument("--out", type=Path, required=True,
                   help="output directory holding notes/ and run-<date>.json; digests/ is written here")
    d.add_argument("--base-url", default=None, help="site root for absolute links (default: config site.base_url)")
    s = sub.add_parser("site", help="build the static site from notes and digests")
    s.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    s.add_argument("--date", default=None, type=_iso_date, help="day shown on index.html (default: today)")
    s.add_argument("--out", type=Path, required=True, help="output directory holding notes/ and digests/")
    s.add_argument("--site", type=Path, default=None, help="site directory (default: <out>/site)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, tomllib.TOMLDecodeError, ConfigError) as exc:
        print(f"daily-pulse: cannot load config {args.config}: {exc}", file=sys.stderr)
        return 1
    register_builtin()
    date = args.date or _date.today().isoformat()
    if args.command == "digest":
        return _digest(args, config, date)
    if args.command == "site":
        return _site(args, date)
    try:
        if args.dry_run:  # touches nothing on disk, so needs no lock
            result = run_pipeline(config, date, args.fixtures, dry_run=True)
            print(json.dumps(result.to_dict(), indent=2))
            return 0
        with RunLock(config.data_dir):
            return _run(args, config, date)
    except (StateError, LockError) as exc:
        print(f"daily-pulse: {exc}", file=sys.stderr)
        return 1
    except LockHeld:
        print(f"daily-pulse: another run is in progress (lock: {config.data_dir / 'run.lock'}); "
              "exiting without changes", file=sys.stderr)
        return 0


def _digest(args, config, date: str) -> int:
    """Rebuild the day's digest from notes on disk plus the run result for source statuses."""
    sources: dict = {}
    now = None  # falls back to the end of `date`
    result_path = args.out / f"run-{date}.json"
    try:
        recorded = json.loads(result_path.read_text(encoding="utf-8"))
        sources = recorded.get("sources", {})
        if not isinstance(sources, dict):
            raise ValueError("'sources' is not an object")
        now = parse_time(recorded.get("generated_at"))
    except FileNotFoundError:
        pass  # no run recorded: statuses are derived from the notes alone
    except (OSError, ValueError, AttributeError) as exc:
        print(f"daily-pulse: ignoring unreadable {result_path}: {exc}", file=sys.stderr)
        sources = {}
    base_url = config.base_url if args.base_url is None else args.base_url
    try:
        write_digest(args.out / "notes", args.out / "digests", date, sources=sources, base_url=base_url, now=now)
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, say so cleanly rather than a traceback
        print(f"daily-pulse: cannot write digest under {args.out / 'digests'}: {exc}", file=sys.stderr)
        return 1
    return 0


def _site(args, date: str) -> int:
    site = args.site or args.out / "site"
    try:
        build_site(args.out, site, date)
    except Exception as exc:  # noqa: BLE001 - say so cleanly rather than a traceback
        print(f"daily-pulse: cannot build site at {site}: {exc}", file=sys.stderr)
        return 1
    return 0


def _preflight_out(out: Path) -> None:
    """Prove we can really write to `out` before any item is consumed."""
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=out, prefix=".write-test.", delete=True) as probe:
        probe.write(b"ok")


def _write_result(out: Path, date: str, payload: dict) -> None:
    """Write run-<date>.json atomically. An earlier file for the same date is kept as
    run-<date>.<n>.json, but only once the new file is fully written, so a failed write
    never loses the earlier result. A rerun keeps the date's first `generated_at`, so the digest's
    age reference (and with it the diamond callout) does not move when a day is run again."""
    target = out / f"run-{date}.json"
    staged = out / f".run-{date}.json.new"
    try:
        first = json.loads(target.read_text(encoding="utf-8")).get("generated_at")
    except (OSError, ValueError, AttributeError):  # no earlier file, or an unreadable one
        first = None
    if isinstance(first, str) and first:
        payload = {**payload, "generated_at": first}
    try:
        atomic_write_json(staged, payload)
        if target.exists():
            n = 1
            while (out / f"run-{date}.{n}.json").exists():
                n += 1
            aside = out / f"run-{date}.{n}.json"
            try:
                os.link(target, aside)  # keep the old content aside
            except OSError:  # filesystem without hard links
                shutil.copy2(target, aside)
        os.replace(staged, target)  # atomic: the canonical file is never missing
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _run(args, config, date: str) -> int:
    try:  # fail before any item is consumed if the output can't be written
        _preflight_out(args.out)
    except OSError as exc:
        print(f"daily-pulse: cannot write output to {args.out}: {exc}", file=sys.stderr)
        return 1
    log_dir = config.data_dir / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / f"run-{date}.log")
    except OSError as exc:
        print(f"daily-pulse: cannot write run log in {log_dir}: {exc}", file=sys.stderr)
        return 1
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("daily_pulse")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        result = run_pipeline(config, date, args.fixtures)
    finally:
        logger.removeHandler(handler)
        handler.close()
    payload = result.to_dict()
    try:
        _write_result(args.out, date, payload)
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, don't lose the result
        # State is already committed, so don't lose what this run found: keep it locally.
        saved = log_dir / f"run-{date}.unsaved.json"
        n = 1
        while saved.exists():  # never overwrite an earlier unsaved result
            saved = log_dir / f"run-{date}.unsaved.{n}.json"
            n += 1
        try:
            atomic_write_json(saved, payload, default=str)
            where = f"this run's result was saved to {saved}"
        except Exception as exc2:  # noqa: BLE001
            where = f"the result could not be saved locally either ({exc2})"
        print(f"daily-pulse: cannot write result to {args.out}: {exc}; {where}", file=sys.stderr)
        return 1
    if result.error:
        print(f"daily-pulse: run aborted: {result.error}", file=sys.stderr)
        return 1
    return 0
