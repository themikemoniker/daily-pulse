"""Command line entry point."""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date as _date
from pathlib import Path

from daily_pulse.config import DEFAULT_CONFIG, load_config
from daily_pulse.runner import run_pipeline


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="daily-pulse")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run the pipeline once")
    r.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    r.add_argument("--date", default=None, help="run date, YYYY-MM-DD (default: today)")
    r.add_argument("--out", type=Path, required=True, help="output directory")
    r.add_argument("--fixtures", type=Path, default=None, help="read recorded inputs, no network")
    r.add_argument("--dry-run", action="store_true", help="write no state or output")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = load_config(args.config)
    date = args.date or _date.today().isoformat()
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
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"run-{date}.json").write_text(json.dumps(result.to_dict(), indent=2))
    return 0
