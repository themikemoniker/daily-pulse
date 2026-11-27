---
title: "Mastering Claude Code in 30 minutes"
channel: "Anthropic"
video_id: "6eBSHbLKuN0"
url: "https://www.youtube.com/watch?v=6eBSHbLKuN0"
published: "2025-05-22"
duration: "28:07"
views: 1869225
rating: 6
tags: ["claude-code", "harness", "permissions", "claude-md", "sdk", "parallel-sessions", "onboarding"]
---

# Mastering Claude Code in 30 minutes

## Key takeaways
- Onboard people by having them ask questions about the codebase first; Anthropic says this cut technical onboarding from 2-3 weeks to 2-3 days.
- Ask the agent to plan and get approval before writing code; no special mode is needed, just say so.
- Agents improve sharply when they can check their own work (unit tests, Puppeteer or simulator screenshots) and iterate two or three times.
- Put shared context in a concise, version-controlled CLAUDE.md, with nested files per directory and a managed enterprise-level file.
- Permissions are layered (enterprise policy, project, user): organisations can pre-approve safe commands and block URLs or commands that users cannot override.
- Bash safety relies on read-only command detection, static analysis of combined commands, and allow/deny lists at several levels.
- The headless -p flag is the same SDK as the CLI and works as a Unix-style tool in CI, incident response and pipelines, with JSON output.
- Power users run many parallel sessions using separate repo copies or git worktrees, often over SSH and tmux.

## The argument, beat by beat
- [01:26] Positions Claude Code as a fully agentic assistant that works in any IDE or terminal and does not force a workflow.
- [02:56] Initial setup: terminal config, theme, GitHub app, allowed tools list, and voice dictation for prompts.
- [04:13] Start with codebase Q&A, git history analysis and GitHub issues; no indexing, code stays local.
- [07:56] Moving to edits: a small toolset, plan-first prompting, and the commit-push-PR shorthand.
- [09:55] Connect team tools via CLIs and MCP servers; give the agent feedback loops such as tests and screenshots.
- [12:17] Context management with CLAUDE.md, nested files, slash commands, and the settings hierarchy including enterprise policy.
- [18:45] Keyboard shortcuts: auto-accept, hash to remember, bash mode, escape to interrupt, resume.
- [20:53] The headless SDK (-p) used in CI, incident response and Unix-style pipelines.
- [22:37] Advanced use: multiple parallel Claude sessions via worktrees, repo copies and tmux.
- [23:24] Q&A on securing bash commands, multimodal input, why a CLI over an IDE, and internal ML usage.

## Rating

**6/10** — Official, concrete coverage of harness setup, permission guard rails, feedback loops and parallel sessions, but it is a general Claude Code intro with nothing on evals, routing, cost or maker-checker, and nothing specific to accounting firms.

## Verdict

skim
