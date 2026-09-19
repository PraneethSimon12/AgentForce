# AgentForge — Build Plan & Wire Contract

**Governs:** the wire format and the build order. Behaviour is governed by `product-spec.md`,
design by `architecture.md`. On conflict: **behaviour > design > wire format**.

Part 1 is the sequence. Part 2 is the contract — the exact bytes on the wire, fixed here so the
frontend, the eval harness and the tests can be written against it before the server exists.

---

# Part 1 — Build sequence

Each phase has an **exit criterion**: a demonstrable behaviour, not a set of merged files. If the
exit criterion cannot be demonstrated, the phase is not done, regardless of how much code exists.

## v0 — The loop, honestly · weeks 1–3

Prove the ReAct loop in memory, with no infrastructure at all.

| # | Task | Teaches |
| --- | --- | --- |
| 0.1 | `settings.py`, app factory, `/healthz`. Nothing else in `main.py`. | Lifespan, single config read |
| 0.2 | `core/ports.py` — the Protocols, before any implementation of them | Dependency inversion, concretely |
| 0.3 | `ToolSpec` + registry; `model_json_schema()` → `input_schema` | Pydantic as schema generator |
| 0.4 | Two toy tools: `calculator`, `now`. Both `READ_ONLY`, both `INLINE`. | The tool contract, minus IO |
| 0.5 | `FakeLLM` — scripted responses, including `tool_use` blocks | Determinism as a testing strategy |
| 0.6 | The loop against `FakeLLM`: call → tool → result → `end_turn` | The block protocol, `stop_reason` |
| 0.7 | `policy.py`: step cap + token budget → `BUDGET_EXCEEDED` | Cost guardrails as code (CLAUDE.md §7) |
| 0.8 | Real `AnthropicClient` behind the same port. One live smoke test, opt-in. | That the port was the right shape |

**Exit:** `pytest` is green with no network, no Docker, no model weights, and the loop completes a
two-tool run against `FakeLLM`. Then the *same loop object* completes a real run against
`claude-opus-5` with only the adapter swapped. If that swap needs a single change inside
`core/`, the boundary is wrong and we fix it before v1.

**Cost gate:** the step cap and token budget ship in this phase, before the loop ever meets a real
API key. A runaway loop is the most expensive bug available to us.

## v1 — Make it durable · weeks 4–7

| # | Task | Teaches |
| --- | --- | --- |
| 1.1 | Alembic; `runs`, `run_steps`, `run_messages`. Review the autogenerate by hand. | Schema as a contract |
| 1.2 | `RunStore` over SQLAlchemy 2.0 async; commit one row per step | Transaction boundaries |
| 1.3 | Replay: committed steps → the exact `messages` array, byte-identical | Why raw blocks are stored, not text |
| 1.4 | Leases: `SELECT ... FOR UPDATE SKIP LOCKED`, TTL, renewal in-loop | Atomic claim vs check-then-act |
| 1.5 | `POST /runs/{id}/resume` | The state machine earning its keep |
| 1.6 | `tool_invocations` ledger + `effect_class` enforcement | The t1–t3 crash window (arch §6) |
| 1.7 | Celery app; `DURABLE` tools dispatched with `acks_late=True` | At-least-once + idempotency |
| 1.8 | Per-step timeout; bounded retries recorded in `attempt` | Layered retry budgets (D-009) |

**Exit — the headline test.** An integration test that starts a run with a `DURABLE` tool,
`SIGKILL`s the worker mid-tool, restarts it, and asserts: the run completes, the tool executed
**exactly once** (ledger + a side-effect counter table), and no duplicate step rows exist. This
test is the resume bullet. Write it before the code it verifies.

Also: kill during the LLM call (must re-issue), kill after commit (must not repeat), two workers
racing one run (exactly one claims), an `UNSAFE` tool interrupted mid-flight (must stop for review,
never auto-retry).

## v2 — Make it visible · weeks 8–10

| # | Task | Teaches |
| --- | --- | --- |
| 2.1 | `EventBus` over Redis Streams; `XADD` on every lifecycle event | Log-with-IDs vs fire-and-forget |
| 2.2 | SSE endpoint, hand-rolled framing, heartbeats | The SSE format, properly |
| 2.3 | `Last-Event-ID` → `XRANGE` replay, then tail | Resumable streams |
| 2.4 | Token streaming from `messages.stream()` → bus, never → Postgres | Two stores, two jobs |
| 2.5 | Prometheus: TTFT, step latency, tokens/run, tool error rate, retries | What to measure and why |
| 2.6 | `frontend/index.html` — `EventSource`, ~200 lines, no framework | Proving it end to end |

**Exit:** kill the browser tab mid-answer, reopen it, and the answer completes with **no missing
and no duplicated tokens**. Record TTFT p95 in `eval-report.md` (N1).

## v3 — Make it useful · weeks 11–14

Strictly incremental, and **each step is measured before the next begins** — otherwise "hybrid RAG
improved things" is unfalsifiable.

| # | Task | Measured before proceeding |
| --- | --- | --- |
| 3.1 | Ingestion, chunking, `documents` + `chunks` | Chunk size/overlap sweep — recall@10 per setting |
| 3.2 | `Embedder` port + local `bge-small`; HNSW index | Index build time; `EXPLAIN ANALYZE` proves index use |
| 3.3 | Dense retrieval + a `retrieve` tool | **Baseline** recall@10, nDCG@10, p95 |
| 3.4 | Lexical `tsvector` + GIN | Lexical-only numbers, same metrics |
| 3.5 | RRF fusion | Hybrid vs both baselines |
| 3.6 | Cross-encoder rerank, off the event loop | Δ quality **and** Δ p95 — this is the latency cost |
| 3.7 | Citation IDs through context → answer → resolution | % answers with valid citations |

**Exit:** the ablation table in `eval-report.md` has a row for every one of dense-only,
lexical-only, hybrid, hybrid+rerank. **D-007 (`ts_rank` vs BM25) must be resolved here** — no
resume wording is finalised until it is.

## v4 — Make it defensible · weeks 15–18

| # | Task |
| --- | --- |
| 4.1 | Golden set: 50 questions, known-relevant chunk IDs, held-out slice reserved from day one |
| 4.2 | Deterministic metrics: recall@k, nDCG@10, MRR |
| 4.3 | LLM-as-judge: fixed rubric, structured output, randomised order, cheaper judge model |
| 4.4 | `make eval` → JSON report; the ablation table regenerates itself |
| 4.5 | CI gate on `main` only; regression beyond threshold fails the build |
| 4.6 | `delegate` tool: child runs, budget slicing, per-agent tool allowlists, depth cap |
| 4.7 | Load test: concurrent runs, SSE connection ceiling, retrieval under load |

**Exit:** a regression is caught by CI on a deliberately-worsened prompt (verify the gate actually
gates — an eval harness that has never failed is not known to work). Multi-agent run visible as a
parent with children, each with its own budget and its own stream.

---

# Part 2 — Wire contract

Versioned under `/v1`. JSON only. All timestamps RFC 3339 UTC. All IDs UUIDv4 as strings.

## 2.1 Errors

One envelope, everywhere:

```json
{
  "error": {
    "code": "BUDGET_EXCEEDED",
    "message": "Run exceeded its token budget of 120000 at step 9.",
    "run_id": "3f2a...",
    "step_idx": 9
  }
}
```

`code` is a stable machine-readable enum — clients switch on it, and it never changes wording.
`message` is for humans and may change freely.

| Code | HTTP | Meaning |
| --- | --- | --- |
| `VALIDATION_ERROR` | 400 | Request body failed schema validation |
| `RUN_NOT_FOUND` | 404 | Unknown `run_id` |
| `RUN_NOT_RESUMABLE` | 409 | Run is terminal, or its lease is held and live |
| `AGENT_NOT_FOUND` | 400 | No such roster entry |
| `BUDGET_EXCEEDED` | 200 (terminal state, not an HTTP failure) | Step or token cap hit |
| `TOOL_TIMEOUT` | — (step-level, surfaced in events) | A step exceeded its timeout |
| `NEEDS_REVIEW` | 200 (terminal state) | `UNSAFE` tool interrupted; a human must resolve |
| `UPSTREAM_REFUSAL` | 200 (terminal state) | Model returned `stop_reason: "refusal"` |
| `RATE_LIMITED` | 429 | Our own per-tenant limit on run creation |

`BUDGET_EXCEEDED` and `UPSTREAM_REFUSAL` are **run outcomes, not HTTP errors** — the API call that
created the run already succeeded. This distinction is deliberate; conflating them is how clients
end up retrying things that will never succeed.

## 2.2 Runs

### `POST /v1/runs` → `202 Accepted`

```json
{
  "agent": "researcher",
  "input": { "query": "How does admission control prevent queue jumping?" },
  "options": { "max_steps": 12, "token_budget": 120000, "effort": "medium" },
  "tenant_id": "acme"
}
```

`Idempotency-Key` header optional; replaying the same key returns the original run rather than
creating a second. `options` fields all default from `settings.py` and are clamped to the
configured ceilings — a client cannot raise its own budget.

```json
{
  "run_id": "3f2a8c1e-...",
  "status": "QUEUED",
  "stream_url": "/v1/runs/3f2a8c1e-.../events",
  "created_at": "2026-08-20T09:14:02Z"
}
```

Returns in single-digit milliseconds. The run has not started; it has been *recorded*. (arch §3.)

### `GET /v1/runs/{run_id}` → `200`

```json
{
  "run_id": "3f2a8c1e-...",
  "parent_run_id": null,
  "agent": "researcher",
  "status": "COMPLETED",
  "input": { "query": "..." },
  "output": {
    "answer": "Admission control issues a short-TTL token ...",
    "citations": [
      { "chunk_id": "9c1f-...", "document_uri": "docs/design.md", "ordinal": 12,
        "quote": "the token is validated at the booking layer" }
    ]
  },
  "usage": { "tokens_in": 18422, "tokens_out": 1201, "steps": 6, "cache_read_tokens": 15900 },
  "children": ["7b0e-..."],
  "created_at": "...", "completed_at": "..."
}
```

`cache_read_tokens` is surfaced because it is the prompt-cache health signal. Zero across repeated
runs means something is invalidating the prefix, and that shows up on the bill before it shows up
anywhere else.

`?include=steps` appends the full step list — the audit trail, and the debugging surface that
replaces Django's admin (D-001).

### `POST /v1/runs/{run_id}/cancel` → `202`

Cooperative: sets the cancel flag, which `policy.check()` observes at the next step boundary. It
does **not** kill an in-flight tool. A cancel during a 30-second tool takes effect when that tool
returns; the response says so via `"effective_at": "next_step_boundary"` rather than pretending
the run stopped instantly.

### `POST /v1/runs/{run_id}/resume` → `202`

Resumes a `PAUSED` run, or one whose lease expired. `409 RUN_NOT_RESUMABLE` if terminal or
actively leased. Idempotent: resuming a running run is a no-op, not an error.

### `GET /v1/runs?status=&agent=&limit=&cursor=` → `200`

Cursor pagination on `(created_at, id)`. Offset pagination is wrong here for the usual reason —
rows are inserted while you page.

## 2.3 Streaming — `GET /v1/runs/{run_id}/events`

`Accept: text/event-stream`. Headers on the response: `Cache-Control: no-cache`,
`Connection: keep-alive`, **`X-Accel-Buffering: no`** (without which a proxy buffers the stream and
the client sees nothing until the run ends — CLAUDE.md §8).

```
id: 1755680042123-0
event: token
data: {"step_idx":3,"text":"Admission"}

: ping

id: 1755680042456-0
event: tool.call
data: {"step_idx":4,"tool":"retrieve","input":{"query":"admission control"},"effect_class":"READ_ONLY"}
```

**`id:` is the Redis stream entry ID, verbatim.** On reconnect the browser sends
`Last-Event-ID: 1755680042123-0` automatically, and the server does `XRANGE (that-id +` before
tailing. No sequence numbers of our own to keep in sync. (D-005.)

| `event` | `data` | Notes |
| --- | --- | --- |
| `run.started` | `{agent, budget}` | Always first |
| `step.started` | `{step_idx, kind}` | |
| `token` | `{step_idx, text}` | Text deltas only |
| `thinking` | `{step_idx, text}` | Only when `display: "summarized"` is on |
| `tool.call` | `{step_idx, tool, input, effect_class}` | Emitted **before** execution |
| `tool.result` | `{step_idx, tool, ok, duration_ms, preview}` | `preview` is truncated; full output is in the step row |
| `citation` | `{chunk_id, document_uri, ordinal}` | As citations are resolved |
| `step.completed` | `{step_idx, tokens_in, tokens_out}` | The durability marker: this step is committed |
| `delegate.started` | `{child_run_id, agent, budget}` | Client may open a second stream |
| `run.completed` | `{output, usage}` | Terminal |
| `run.failed` | `{error: {code, message}}` | Terminal; includes `BUDGET_EXCEEDED`, `NEEDS_REVIEW` |
| `heartbeat` | comment line `: ping` | ~15s; not a real event, keeps proxies from closing |

Clients must **ignore unknown event types** — that is the forward-compatibility rule that lets us
add events without a version bump.

## 2.4 Documents

### `POST /v1/documents` → `202`

```json
{ "uri": "docs/design.md", "title": "QueueFair Design", "content": "...", "tenant_id": "acme" }
```

```json
{ "document_id": "...", "status": "INGESTING", "sha256": "..." }
```

Chunking and embedding are Celery work, not request work. Re-posting identical content is a no-op
by `sha256` — re-ingestion must not silently duplicate chunks and quietly wreck recall metrics.

### `GET /v1/documents/{id}` → `200` — status, chunk count, ingestion errors.

## 2.5 Search — the debug endpoint that makes retrieval explainable

### `POST /v1/search` → `200`

```json
{ "query": "how does admission control work", "top_k": 50, "rerank": true, "explain": true }
```

```json
{
  "results": [
    { "chunk_id": "...", "document_uri": "docs/design.md", "ordinal": 12,
      "text": "...", "final_rank": 1,
      "dense_rank": 3, "lexical_rank": 1, "rrf_score": 0.0312, "rerank_score": 8.41 }
  ],
  "timings_ms": { "embed": 11, "dense": 8, "lexical": 4, "fuse": 1, "rerank": 240 }
}
```

This endpoint exists for one reason: **every stage's contribution must be visible per query.**
`dense_rank` next to `lexical_rank` next to `rerank_score` is how "the rerank moved the right
chunk from #7 to #1" becomes a screenshot instead of an assertion. `timings_ms` is where the
cross-encoder's dominance of p95 becomes undeniable (arch §10). It is also the harness the eval
suite calls, so it can never rot.

## 2.6 Operations

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz` | Liveness. No dependency checks — a health check that pings the DB turns one outage into two. |
| `GET /readyz` | Readiness: Postgres reachable, Redis reachable, models loaded. |
| `GET /metrics` | Prometheus. Multiprocess mode under multiple uvicorn workers. |

**Metrics that exist from v2:** `agentforge_run_duration_seconds`,
`agentforge_step_duration_seconds{kind}`, `agentforge_ttft_seconds`,
`agentforge_tokens_total{direction}`, `agentforge_tool_calls_total{tool,ok}`,
`agentforge_tool_retries_total{tool}`, `agentforge_runs_active`,
`agentforge_sse_connections`, `agentforge_retrieval_seconds{stage}`,
`agentforge_cache_read_tokens_total`.

The last one is a cost metric, not a performance metric, and it belongs on the same dashboard —
CLAUDE.md §7 is only enforceable if it is observable.
