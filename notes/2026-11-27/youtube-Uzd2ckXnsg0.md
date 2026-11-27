---
title: "CLI-Anything Just Brought Claude Code Into The Future"
channel: "Chase AI"
video_id: "Uzd2ckXnsg0"
url: "https://www.youtube.com/watch?v=Uzd2ckXnsg0"
published: "2026-03-11"
duration: "7:48"
views: 88314
rating: 4
tags: ["claude-code", "cli", "mcp", "agent-harness", "tooling", "demo"]
---

# CLI-Anything Just Brought Claude Code Into The Future

## Key takeaways
- CLI-Anything generates an agent-usable CLI for any open-source codebase by reading its source.
- The pipeline has seven steps: analyse, design, plan tests, write tests, document, publish, then refine.
- Prebuilt CLIs exist for Blender, Inkscape and Audacity, with test counts shown (Blender has 208).
- Setup is two plugin commands in Claude Code, a git clone of the target repo, then one command pointing at it.
- The draw.io build took about 20 minutes and ended with a success-criteria and test report.
- The video argues CLIs beat MCP because the agent already lives in the terminal with no middleman or config overhead.
- The generated wrapper is called an agent harness, which loosely touches harness engineering.
- Nothing is said about verifying generated CLIs independently, cost, model choice or security of running them.

## The argument, beat by beat
- [00:00] Claims the Claude Code scene is moving from MCP tools to CLIs because of lower overhead and no middleman.
- [00:39] Introduces CLI-Anything from the HKU data intelligence lab, the group behind LightRAG and Nanobot.
- [01:06] Explains the pitch: point it at open-source software and get an agent-controllable CLI, comparable to the Google Workspace CLI.
- [02:54] Describes the automatic seven-step pipeline from code analysis through tests, docs, publishing and refinement.
- [03:38] Shows prebuilt CLIs for Blender, Inkscape and Audacity along with their test counts.
- [04:19] Demo setup: install the plugin from the marketplace, reload plugins, clone the draw.io repo.
- [05:38] Runs the build command, which takes about 20 minutes and finishes with a success-criteria and test summary.
- [06:11] Prompts Claude Code to use the new CLI to draw a SaaS back-end diagram, which comes out well laid out.
- [07:02] Closes by urging viewers to try it on other open-source projects and promotes his course.

## Quotes

> Every software becomes instantly agent controllable through structured CLI.

## Rating

**4/10** — Touches harness building and auto-generated tests in passing, but has no evals, orchestration, routing or accounting-firm guardrail content, and the generated tools are unverified.

## Verdict

skim
