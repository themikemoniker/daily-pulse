---
title: "Spikee #1 - LLM Benchmarking/Testing Tool for Prompt Injection"
channel: "Donato Capitella"
video_id: "bsBnQlo28tg"
url: "https://www.youtube.com/watch?v=bsBnQlo28tg"
published: "2025-02-03"
duration: "29:27"
views: 10333
rating: 6
tags: ["prompt-injection", "evals", "guardrails", "benchmarking", "security", "spikee"]
---

# Spikee #1 - LLM Benchmarking/Testing Tool for Prompt Injection

## Key takeaways
- Treat prompt injection (attacking an app to hit other users or data) as separate from jailbreaking (breaking model alignment); test for the first one in client apps.
- Build a custom dataset for the specific use case (summarise, Q&A) rather than relying on generic benchmarks; the bundled dataset is deliberately basic.
- Test the model alone, the input guardrail alone, and the full application pipeline; each answers a different question.
- Compare configurations: plain prompt, system message only, data markers (spotlighting) only, and both. Success is measured by whether a canary string appears in the output.
- In this benchmark Claude models resisted best even without mitigations, o1 did well, and DeepSeek R1 and V3 did worst.
- Summarisation tasks were more exposed to injected instructions than direct question answering.
- Guardrails must be judged on false positives as well as detection; a 30-document benign set was used. InjectGuard had 100% detection with 3.3% false positives, and Prompt Guard had a worse false-positive rate.
- Output handling matters: unencoded model output can become XSS or markdown-image data exfiltration.

## The argument, beat by beat
- [00:00] Spikee is introduced as a consolidation of two years of client LLM security testing scripts, aimed at pentesters and developers.
- [02:47] Jailbreaking versus prompt injection: the former attacks the model's alignment, the latter exploits an application to attack users or organisations.
- [05:26] Email summarise-button example showing an injected instruction producing XSS or markdown exfiltration, versus a safe response that treats it as data.
- [11:51] Mitigations to evaluate: a system message telling the model to ignore document instructions, and spotlighting with data markers.
- [13:39] Use cases: standalone LLM benchmark, custom datasets, guardrail testing and full application pipeline testing.
- [15:36] Targeted-12-2024 dataset: 1,912 English entries across summary and Q&A, with and without system message and markers, using several jailbreak patterns and goals.
- [20:32] Benchmark results by model and configuration; Claude and o1 strongest, DeepSeek weakest; caveat that the dataset is basic.
- [26:02] Guardrail benchmark of InjectGuard, Azure Prompt Shields, Bedrock guardrails and Meta Prompt Guard on detection and false positives.

## Rating

**6/10** — Concrete eval and guardrail testing methodology with model comparisons, useful for a consultant's guard rails and evals work, but security-focused and with no multi-agent, maker-checker or accounting angle; the video is also dated (Feb 2025).

## Verdict

skim
