---
title: "Stop Wasting Money on AI APIs: Use Cloudflare Queues"
channel: "Cloudflare Developers"
video_id: "ZDv4iYaLbpI"
url: "https://www.youtube.com/watch?v=ZDv4iYaLbpI"
published: "2026-04-23"
duration: "5:09"
views: 7677583
rating: 5
tags: ["cost", "batch-api", "cloudflare", "queues", "reliability", "anthropic", "openai"]
---

# Stop Wasting Money on AI APIs: Use Cloudflare Queues

## Key takeaways
- Batch APIs from OpenAI and Anthropic are discounted about 50%, so route any work where nobody is waiting on a response through them.
- Producer pattern: the web app writes the record to a database, enqueues a message and returns immediately, keeping the user-facing path fast.
- The queue consumer receives messages in batches, with a configurable batch size and timeout (whichever is hit first), and submits them as one batch request.
- Retries and a dead letter queue (max three retries here) absorb rate limits, overloads and transient LLM API errors so no message is lost.
- Batch APIs are asynchronous, so a cron trigger polls for in-progress batch IDs and writes completed results back to the database.
- The demo task is classification (category, priority, summary), a typical cheap, latency-tolerant workload suited to batching.
- Queues are now on the Workers free plan (10k operations per day); paid is 1M per month included, then $0.40 per million.
- Acknowledging messages explicitly after successful submission is key to the at-least-once guarantee.

## The argument, beat by beat
- [00:00] Pitch: Queues are now on the free plan, and pairing them with batch APIs gives 50% off tokens when users are not waiting.
- [00:30] Demo of a feedback classifier app; submissions return instantly and show as in progress in D1.
- [01:22] Cron trigger in Wrangler polls Anthropic for in-progress batch IDs and writes completed results back to D1.
- [01:52] Producer code: the API handler creates an ID, inserts into D1, sends to the queue and returns JSON.
- [02:17] Consumer config: batch size three, 60s timeout, max three retries; it builds requests and sends them to the Anthropic API, then acks.
- [03:05] Results arrive: categories, priorities and summaries are upserted into D1.
- [03:46] Reliability: retries with backoff and a dead letter queue mean messages are never lost when LLM APIs fail.
- [04:30] Summary and pricing: free tier 10k ops per day, paid 1M per month then 40 cents per million.

## Quotes

> If a user isn't actively waiting on an AI response, don't call the model online.

## Rating

**5/10** — Concrete cost-saving pattern for model routing and cost (batch APIs plus queues with retries), but it is Cloudflare-specific, shallow, and says nothing on evals, maker-checker, orchestration or accounting-firm guard rails.

## Verdict

skim
