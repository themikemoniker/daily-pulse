"""Command line entry point."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tomllib
from datetime import date as _date
from pathlib import Path

from daily_pulse.config import DEFAULT_CONFIG, load_config
from daily_pulse.lock import LockHeld, RunLock
from daily_pulse.runner import run_pipeline
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"daily-pulse: cannot load config {args.config}: {exc}", file=sys.stderr)
        return 1
    date = args.date or _date.today().isoformat()
    try:
        if args.dry_run:  # touches nothing on disk, so needs no lock
            result = run_pipeline(config, date, args.fixtures, dry_run=True)
            print(json.dumps(result.to_dict(), indent=2))
            return 0
        with RunLock(config.data_dir):
            return _run(args, config, date)
    except StateError as exc:
        print(f"daily-pulse: {exc}", file=sys.stderr)
        return 1
    except LockHeld:
        print(f"daily-pulse: another run is in progress (lock: {config.data_dir / 'run.lock'}); "
              "exiting without changes", file=sys.stderr)
        return 0


def _write_result(out: Path, date: str, payload: dict) -> None:
    """Write run-<date>.json atomically; an earlier file for the same date is kept as
    run-<date>.<n>.json rather than overwritten."""
    target = out / f"run-{date}.json"
    if target.exists():
        n = 1
        while (out / f"run-{date}.{n}.json").exists():
            n += 1
        target.rename(out / f"run-{date}.{n}.json")
    atomic_write_json(target, payload)


def _run(args, config, date: str) -> int:
    try:  # fail before any item is consumed if the output can't be written
        args.out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"daily-pulse: cannot write output to {args.out}: {exc}", file=sys.stderr)
        return 1
    log_dir = config.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / f"run-{date}.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("daily_pulse")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        result = run_pipeline(config, date, args.fixtures)
    finally:
        logger.removeHandler(handler)
        handler.close()
    _write_result(args.out, date, result.to_dict())
    return 0
