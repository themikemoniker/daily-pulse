---
title: "STOP Using Bypass Permissions, Use This New Feature Instead"
channel: "Nate Herk | AI Automation"
video_id: "pkSxISewcw8"
url: "https://www.youtube.com/watch?v=pkSxISewcw8"
published: "2026-03-24"
duration: "5:41"
views: 119543
rating: 6
tags: ["claude-code", "permissions", "auto-mode", "guardrails", "harness", "prompt-injection", "cost"]
---

# STOP Using Bypass Permissions, Use This New Feature Instead

## Key takeaways
- Auto mode is a middle path between ask-before-edits (constant interruptions) and bypass permissions (no guardrails).
- A classifier checks each tool call for destructive actions, sensitive data exposure and prompt injection before it runs.
- Risky actions are blocked and Claude tries a different approach instead of stalling the session.
- Sessions cost slightly more, likely because an extra model call reviews every tool call.
- It reduces risk but does not eliminate it; Anthropic recommends isolated environments.
- Allow/deny lists in the local settings file remain, in the presenter's view, the best option in some cases because they are deterministic and per-project.
- Research preview, Team plan only; an admin enables it in organization settings, then it is launched with an enable-auto-mode flag.

## The argument, beat by beat
- [00:00] Introduces auto mode as a safer alternative to dangerously skipping permissions for long-running work.
- [00:09] Shows the default ask-before-edits mode and how repeated approval prompts interrupt the workflow.
- [00:49] Explains bypass permissions mode: no interruptions, but risky if unattended.
- [01:08] Describes his earlier workaround, a local settings file with allow, deny and ask lists, usable per project or globally.
- [01:38] Demonstrates switching to auto mode in VS Code and via the CLI flag, and reads the in-app description.
- [02:28] Frames auto mode as fewer interruptions with less risk than skipping all permissions; a classifier reviews each tool call.
- [02:56] Notes the added cost, guessing it comes from an AI check before every tool call.
- [03:11] Covers availability: research preview, Team users only, enabled in organization settings, and how to disable it.

## Quotes

> Claude checks each tool for risky actions and prompt injection before executing.
> it reduces risk but it does not eliminate it entirely

## Rating

**6/10** — A clear, concrete look at a classifier-gated permission mode, which is relevant to harness guard rails and cost trade-offs, but it is shallow, with no evals, no firm-specific guidance and no depth on how the classifier performs.

## Verdict

skim
