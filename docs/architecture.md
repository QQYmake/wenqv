# Architecture

## Outcome and Boundary

This design implements one deployable FastAPI process and one React SPA. The execution policy lives in a framework-independent Agent Core. HTTP, SQLite, Redis, files, and the remote LLM are adapters.

This is a **hexagonal boundary**（六边形架构边界）, not a microservice split. It improves testability without adding queues, distributed workers, or extra deployment units.

The quick architecture map is [`architecture.mmd`](architecture.mmd). The exported SVG and PNG are generated from the same Mermaid source.

![Blue Lake Agent architecture](architecture.svg)

## Dependency Rules

1. `server.agent` owns ReAct orchestration, context policy, tool contracts, and Skill semantics. It must not import FastAPI or a concrete database.
2. `server.storage` implements the persistence port. SQLite is the source of truth; Redis may speed up reads, but correctness must not depend on it.
3. `server.api` translates HTTP requests into application calls and application events into SSE frames. It does not decide Agent policy.
4. `server.main` is the **composition root**（依赖组装入口）. Concrete implementations meet only there.
5. `web` depends on the published REST/SSE contract, not backend implementation details.

## Request Lifecycle

For each submitted user message, the API registers a cancellation token under `(workspace_id, session_id)`. The Agent persists the user message, injects explicitly selected Skills once, asks the main model for the next action, executes requested tools under timeout and size limits, feeds normalized results back, and repeats until it receives a final answer or reaches `max_turns`.

Every visible step is emitted as a typed event. Important state is persisted before the terminal `done` event.

Native `EventSource` cannot send a POST body, so the browser uses `fetch()` and parses the response stream as SSE. This keeps the requested `POST /api/chat` contract while preserving incremental rendering.

## Persistence Model

The minimum durable model is:

- `sessions`: workspace-scoped identity, title, and timestamps.
- `messages`: ordered conversation records with role, content, kind, tool-call linkage, and JSON metadata.
- Skill injections are marked message records. Their metadata supplies the per-session deduplication key.
- Context summaries are marked records. In this single-store MVP, compacted originals are atomically replaced in SQLite and no longer appear in restored history or later model context.

All queries include `workspace_id`. A deployment without login isolates data by its configured workspace, not by an authenticated user identity.

## Context Control

The context manager estimates tokens with a model-neutral approximation. At `token_budget * summary_trigger_ratio`, it preserves recent messages and sends eligible earlier messages to the summary-role client.

A successful summary replaces those messages in the model-facing context and is persisted when the storage port supports compaction. If the summary request fails, deterministic oldest-first truncation keeps the live chat path working. Tool calls and tool results are kept as indivisible pairs where possible.

`get_client("summary")` owns fallback to `llm.main`, so title generation and context compression do not duplicate configuration policy.

## Failure and Cancellation Policy

- Tool exceptions, timeouts, and oversized results become structured tool results; the model can retry or change strategy.
- Consecutive failures are bounded by a per-tool retry limit, and all reasoning is bounded by total turns.
- Summary/title failures use deterministic fallbacks and never fail the main answer.
- Abort is **cooperative cancellation**（协作式中断）. The API sets the active run's cancellation event; the loop checks it between model and tool boundaries and cancels in-flight tasks where supported.
- Redis failure is treated as a cache miss. SQLite failure is surfaced because it threatens durable correctness.

## Deliberate Limits

This MVP does not add authentication, distributed workers, queues, or multi-process cancellation. A remote public deployment still needs a trusted reverse proxy, TLS, authentication, request-size/rate limits, and an operating-system sandbox for stronger file/tool isolation. These are deployment security controls, not hidden assumptions inside the Agent loop.
