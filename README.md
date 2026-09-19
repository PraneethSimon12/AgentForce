# AgentForge

**A durable, resumable runtime for LLM agents, with hybrid retrieval underneath.**

An agent loop is twenty lines of Python. Everything hard starts on line twenty-one: the loop runs
for minutes, it has side effects, the process gets restarted mid-deploy, and the user is staring
at a spinner. AgentForge is those twenty lines plus the answers.

```
POST /v1/runs ──► 202 {run_id}          the run is recorded, not started
GET  /v1/runs/{id}/events ──► SSE       tokens, tool calls, citations, live
      ⟂ kill -9 the worker              lease expires, another worker resumes
        └──► run completes, the tool having executed exactly once
```

---

## What it does

- **Multi-step ReAct loop** over a typed tool registry. A tool's Pydantic input model *is* its
  JSON Schema — one definition, so the validator and the schema the model plans against cannot
  drift apart.
- **Durable and resumable.** One committed Postgres row per step. Kill the process mid-run and it
  continues on another worker from the last completed step, without repeating side effects.
- **Streaming with replay.** Tokens over SSE, backed by Redis Streams. A client that drops for
  three seconds reconnects with `Last-Event-ID` and loses nothing.
- **Hybrid RAG over pgvector.** Lexical + dense retrieval, fused with reciprocal rank fusion, then
  cross-encoder reranked, with citations resolved back to source chunks.
- **Measured, not asserted.** A golden set, retrieval metrics, and an LLM-as-judge harness in CI —
  because "hybrid retrieval improved quality" is not a claim without the ablation table beside it.

## Status

**v0 — the loop runs.** 122 unit tests, green with no network, no database and no model weights. The documents are ahead of the code on
purpose; writing the design first is how the `ts_rank`-is-not-BM25 problem was caught before it
reached a resume bullet (`docs/decisions.md` D-007).

| Phase | Scope | State |
| --- | --- | --- |
| v0 | The loop, in memory, against a scripted fake LLM | 🟨 code complete, live smoke test not yet run |
| v1 | Durability: checkpoints, leases, idempotency ledger, Celery | ⬜ |
| v2 | Streaming: Redis Streams, SSE with replay, metrics | ⬜ |
| v3 | Retrieval: ingestion, dense → lexical → RRF → rerank, each measured | ⬜ |
| v4 | Evaluation harness in CI, then multi-agent delegation | ⬜ |

## Stack

FastAPI · Pydantic v2 · PostgreSQL 16 + pgvector · Redis 7 · Celery 5 · SQLAlchemy 2.0 ·
Anthropic `claude-opus-5` · Docker Compose · Prometheus + Grafana

**No LangChain, no LlamaIndex, no vector database service.** The orchestration loop is the
subject of the project, so importing a framework that provides it would delete the project. One
Postgres serves as the run store, the lexical index and the vector index — and being able to
explain why that is sufficient is worth more than having operated Pinecone.

## Running it

```bash
cp .env.example .env      # add your ANTHROPIC_API_KEY
make up                   # api + worker + postgres + redis  → http://localhost:8000/docs
make test                 # unit suite: no network, no DB, no model weights
make test-int             # integration suite against real Postgres + Redis
make eval                 # golden set → docs/eval-report.md
```

The unit suite running with no infrastructure is not a convenience — it is the proof that the
`core/` ↔ `adapters/` boundary is intact. If it ever needs Docker, the architecture has rotted.

## Layout

```
app/core/       pure logic — the loop, the registry, RRF. Zero imports from adapters/.
app/adapters/   the IO edge — Anthropic, Postgres, Redis, Celery, the local models.
app/api/        thin HTTP layer: parse, delegate, serialise.
evals/          golden set, metrics, judges.
docs/           see docs/index.md
```

## Documentation

| | |
| --- | --- |
| [`docs/index.md`](docs/index.md) | The doc map — which file answers which question |
| [`docs/product-spec.md`](docs/product-spec.md) | What it does; journeys and edge-case behaviour |
| [`docs/architecture.md`](docs/architecture.md) | The design, and why it is shaped this way |
| [`docs/plan.md`](docs/plan.md) | Build phases and the full wire contract |
| [`docs/decisions.md`](docs/decisions.md) | Append-only decision log |
| [`docs/eval-report.md`](docs/eval-report.md) | Every number, and the run that produced it |
| [`CLAUDE.md`](CLAUDE.md) | How this repo is worked on |

Sibling project: **[QueueFair](../QueueFair)** — a distributed virtual waiting room. Same
conventions, opposite storage decision, and the contrast is deliberate: QueueFair's state is
ephemeral and lives entirely in Redis, AgentForge's is an auditable execution log and lives in
Postgres.

---

Praneeth Simon Katta · [GitHub](https://github.com/) · [LinkedIn](https://linkedin.com/)
