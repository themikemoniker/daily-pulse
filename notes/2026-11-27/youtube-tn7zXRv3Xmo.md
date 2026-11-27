---
title: "DeepSeekV4 + Claude Code = 100X Cheaper"
channel: "Jack Roberts"
video_id: "tn7zXRv3Xmo"
url: "https://www.youtube.com/watch?v=tn7zXRv3Xmo"
published: "2026-04-30"
duration: "16:49"
views: 170002
rating: 6
tags: ["model-routing", "cost", "claude-code", "deepseek", "harness", "maker-checker", "data-privacy"]
---

# DeepSeekV4 + Claude Code = 100X Cheaper

## Key takeaways
- A proxy repo intercepts Claude Code's API calls so the same harness, tools and skills run on DeepSeek V4, a free OpenRouter model, or Claude, in separate terminals.
- Routing heuristic: Claude for creative and visual design, DeepSeek for scripts, algorithms, unit tests and backend grunt work, Codex for code review.
- The 100x cost figure compares a 4-hour Opus session with DeepSeek Flash at a promo price; the speaker admits the gap narrows after the promo ends.
- Skill and tool calling was tested with one Bitly skill and worked; this is a single anecdote, not an eval.
- DeepSeek did well adding a feature inside an existing design system but was weak as the first design artist.
- OpenRouter free models were described as heavily rate limited, so a funded direct DeepSeek API account was preferred.
- Prompts go to China-hosted infrastructure, so do not use it with regulated, client or sensitive data, and never paste API keys or secrets into it.
- A maker-checker pattern appears in passing: one model builds and a different model reviews, using an existing ChatGPT subscription.

## The argument, beat by beat
- [00:00] Pitch: run the Claude Code harness on DeepSeek V4 for roughly 100x lower cost with no rate limits.
- [00:49] DeepSeek V4 overview: large open-weight MIT model with a 1M context window and strong tool calling. Cost comparison of Opus per session against V4 and Flash, with a promo ending May 5.
- [01:48] Benchmarks mentioned (SWE-bench, Terminal-Bench, MCP Atlas), with the caveat that real-world behaviour matters more. Introduces the multi-terminal setup of free model, DeepSeek and Claude side by side.
- [03:00] Setup: DeepSeek platform account with a few dollars of credit, API key, and a proxy repo installed by the agent itself. OpenRouter is rejected as too rate limited.
- [05:25] Testing the shortcut commands, confirming the Flash vs Pro model, and noting the model self-identifies as Claude because of the proxy.
- [06:40] Skill-use test with a Bitly shortener skill, which works.
- [07:38] When to use which model: Claude for creative and UI work, DeepSeek for scripts, tests and algorithmic work, a third model for review.
- [08:50] Privacy and censorship caveats, plus advice not to paste secrets into DeepSeek.
- [10:08] Demo: Claude builds a coffee website from a design-system repo, then DeepSeek adds an ROI calculator matching the existing style.
- [13:58] Free OpenRouter model cycling as an alternative, and tagging GPT Codex in to review the code.
- [15:24] Limitations and guidance: weak first-pass UI, promo pricing distorts the savings, avoid for corporate or regulated data, good for solo dev and prototyping.

## Quotes

> if you are corporate, do not use this
> your prompts go to China

## Rating

**6/10** — Gives a concrete cheap-builder, premium-designer, separate-reviewer routing pattern and a harness-swap method, but cost claims are promo-inflated, there are no evals, and the China-hosted data warning rules it out for accounting-firm client work.

## Verdict

skim
