"""Rates items against the rubric by running `claude -p` as a subprocess.

The prompt (rubric + summarize template, both read from `prompts/` at call time) goes in on
stdin; the reply must be one JSON object, which is validated before it becomes a `Rating`.
"""
from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from daily_pulse.models import Beat, Item, Rating

DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@dataclass
class Summarizer:
    claude_cmd: str = "claude"
    prompts_dir: Path = DEFAULT_PROMPTS_DIR

    def _prompt(self, item: Item, transcript: str) -> str:
        rubric = (Path(self.prompts_dir) / "rubric.md").read_text(encoding="utf-8")
        template = (Path(self.prompts_dir) / "summarize.md").read_text(encoding="utf-8")
        body = json.dumps(item.to_dict(), ensure_ascii=False, indent=2)
        if transcript:
            body += "\n\nTranscript/caption:\n" + transcript
        return rubric.rstrip() + "\n\n" + template.replace("{{ITEM}}", body)

    def _call(self, prompt: str) -> str:
        proc = subprocess.run([*shlex.split(self.claude_cmd), "-p"], input=prompt, text=True,
                              capture_output=True, check=True)
        return proc.stdout

    def rate(self, item: Item, transcript: str = "") -> Rating:
        data = json.loads(self._call(self._prompt(item, transcript)))
        return Rating(
            rating=data["rating"], reason=data["reason"], verdict=data["verdict"],
            tags=data["tags"], summary=data["summary"], takeaways=data["takeaways"],
            beats=[Beat(b["timestamp"], b["text"]) for b in data["beats"]],
            diamond=data["diamond"], diamond_reason=data["diamond_reason"],
        )
