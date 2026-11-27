"""Runs `claude -p` as a hardened subprocess. Shared by the summarizer and the quality triage.

Untrusted item text goes in on stdin, so claude gets no tools, no skills, no MCP servers and no
saved session, runs in an empty scratch directory, and its whole process group is killed on exit.
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import tempfile


class CallFailed(Exception):
    """The claude process failed or timed out; a retry may help."""


class CannotRun(CallFailed):
    """The claude process could not be started at all; a retry cannot help."""


class ClaudeCaller:
    """Mixin: needs `claude_cmd`, `timeout` and `calls_made` attributes on the subclass."""

    def _argv(self) -> list[str]:
        # Item text and transcripts are untrusted, so claude gets no tools (nothing for injected
        # instructions to run), no skills, no MCP servers and no saved session.
        return [*shlex.split(self.claude_cmd), "-p", "--tools", "", "--disable-slash-commands",
                "--strict-mcp-config", "--no-session-persistence"]

    def _call(self, prompt: str) -> str:
        """One `claude -p` run, in its own process group and an empty scratch directory (so it
        loads none of this repo's CLAUDE.md/AGENTS.md). On timeout the whole group is killed:
        helper processes (node, MCP servers) would otherwise hold the pipes open."""
        with tempfile.TemporaryDirectory(prefix="daily-pulse-claude-", ignore_cleanup_errors=True) as scratch:
            try:
                proc = subprocess.Popen(
                    self._argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=scratch, start_new_session=True, encoding="utf-8", errors="replace")
            except (OSError, ValueError) as exc:  # not found, not executable, bad command string
                raise CannotRun(f"cannot run {self.claude_cmd!r}: {exc}") from None
            try:
                stdout, stderr = proc.communicate(prompt, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                self.calls_made += 1
                raise CallFailed(f"timed out after {self.timeout}s") from None
            finally:
                self._kill_group(proc)  # also helpers that outlive a finished claude
        self.calls_made += 1
        if proc.returncode != 0:
            raise CallFailed(f"exit {proc.returncode}: {stderr.strip()[:200]}")
        return stdout

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=5)  # reap and drain; never wait on a stray pipe holder
        except (subprocess.TimeoutExpired, ValueError):
            pass
