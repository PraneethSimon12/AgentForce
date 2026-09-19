# Resume Claims — AgentForge

Every claim my CV makes about this project, mapped to the evidence that supports it and its
current status. **A claim is not allowed to reach the CV until its evidence row points at a real
run recorded in `eval-report.md`.**

This file exists because the failure mode it prevents is specific and expensive: writing the
resume bullet first, building something adjacent to it, and discovering the gap in an interview
while a staff engineer reads the bullet back at me.

**Status key:** ⬜ not started · 🟨 partially true · ✅ true and evidenced · ⚠️ **claim does not
match reality — resolve before sending**

---

## The bullet as currently written

> **AgentForge: Multi-Agent Orchestration & RAG Runtime** | FastAPI, Pydantic, pgvector, Redis, Celery
>
> - Built an async agent runtime in FastAPI executing multi-step tool-calling (ReAct) loops over a
>   typed tool registry — Pydantic models auto-generate the JSON schemas sent to the LLM — with
>   per-step timeouts, bounded retries, and token streaming over SSE.
> - Made agent runs durable and resumable by checkpointing step state to PostgreSQL and dispatching
>   long-running tools to Celery workers with idempotency keys, so a crashed run resumes from its
>   last completed step.
> - Implemented hybrid RAG over pgvector — BM25 + dense retrieval merged with reciprocal rank
>   fusion, then cross-encoder reranking — with citation-tracked answers and a CI eval harness
>   (golden set + LLM-as-judge) guarding regressions.

---

## Claim-by-claim

### Bullet 1 — the runtime

| # | Claim | Evidence required | Phase | Status |
| --- | --- | --- | --- | --- |
| 1.1 | "async agent runtime in FastAPI" | The service runs; async throughout the request path | v0 | 🟨 |
| 1.2 | "multi-step tool-calling (ReAct) loops" | A run with ≥2 tool calls before answering, step trail visible | v0 | 🟨 |
| 1.3 | "typed tool registry" | `ToolSpec` + registry; a tool cannot be registered untyped | v0 | ✅ |
| 1.4 | "Pydantic models auto-generate the JSON schemas sent to the LLM" | `model_json_schema()` output is what lands in the `tools` param — show the request | v0 | 🟨 |
| 1.5 | "per-step timeouts" | A test where a slow tool trips the timeout and the run survives | v1 | ⬜ |
| 1.6 | "bounded retries" | `run_steps.attempt` incrementing; a test proving the bound holds | v1 | ⬜ |
| 1.7 | "token streaming over SSE" | Tokens arrive incrementally; **TTFT p95 recorded** | v2 | ⬜ |

**Status after v0.8 (adapter written, never run).** The `AnthropicClient` exists, is unit-tested
against stubbed SDK objects, and the live smoke test in `tests/integration/` is written — **and
has not been executed, because no API key has been used yet.** Until it runs green:

- **1.1 is 🟨.** The runtime is async end to end and the service starts, but nothing drives a run
  over HTTP yet; `POST /v1/runs` arrives in v1.
- **1.2 is 🟨.** Multi-step tool calling, parallel tool calls and the full step trail are proven
  against `FakeLLM` with 122 unit tests. They are not yet proven against the real API.
- **1.4 stays 🟨** for the same reason: the schema is generated and the request is built, but no
  real request carrying it has been sent.

Running `pytest -m integration` with a key is what moves all three. Nothing here is allowed to
reach ✅ on the strength of a test that has only ever been skipped.

**1.3 is ✅ as of v0.3.** `ToolSpec` is generic in its input model, `effect_class` is a required
field with no default, and an illegal tool name or an empty description is rejected at definition
time. `tests/unit/test_tool_registry.py` covers all of it.

**1.4 is 🟨, not ✅, and the distinction is the whole point of this file.** The *generation* half is
proven: one Pydantic declaration produces both the `minimum`/`maximum` in the schema and the rule
that rejects an out-of-range value, with a test asserting exactly that. The words "**sent to the
LLM**" are not yet true — nothing sends a request, because the Anthropic adapter is v0.8. Until a
real request goes out carrying that schema, the claim is half-earned and stays 🟨.

### Bullet 2 — durability

| # | Claim | Evidence required | Phase | Status |
| --- | --- | --- | --- | --- |
| 2.1 | "checkpointing step state to PostgreSQL" | One committed row per step; `UNIQUE(run_id, idx)` | v1 | ⬜ |
| 2.2 | "dispatching long-running tools to Celery workers" | A `DURABLE` tool executing off-process | v1 | ⬜ |
| 2.3 | "with idempotency keys" | `tool_invocations` ledger; the key computed before execution | v1 | ⬜ |
| 2.4 | **"a crashed run resumes from its last completed step"** | **The `kill -9` test**: run completes, tool ran exactly once, no duplicate steps | v1 | ⬜ |

**2.4 is the strongest claim in the whole bullet** and the one an interviewer will push hardest
on. The follow-up is always the same — *"what happens if you crash between running the tool and
recording that you ran it?"* — and the answer is `architecture.md` §6 plus the effect-class table.
Do not let this test be a happy-path test.

### Bullet 3 — retrieval and evaluation

| # | Claim | Evidence required | Phase | Status |
| --- | --- | --- | --- | --- |
| 3.1 | "hybrid RAG over pgvector" | Dense retrieval on an HNSW index; `EXPLAIN ANALYZE` proves the index is used | v3 | ⬜ |
| 3.2 | **"BM25"** | See ⚠️ **A** below | v3 | ⚠️ |
| 3.3 | "merged with reciprocal rank fusion" | RRF implemented; ablation shows hybrid vs each half | v3 | ⬜ |
| 3.4 | "cross-encoder reranking" | Rerank stage measured for **both** quality delta and latency cost | v3 | ⬜ |
| 3.5 | "citation-tracked answers" | Citations resolve to `document.uri` + `ordinal`; uncited-ID detection works | v3 | ⬜ |
| 3.6 | "CI eval harness (golden set + LLM-as-judge)" | `make eval` in CI; a real regression caught | v4 | ⬜ |
| 3.7 | "guarding regressions" | The gate has **failed at least once on purpose** | v4 | ⬜ |

### Title

| # | Claim | Evidence required | Phase | Status |
| --- | --- | --- | --- | --- |
| T.1 | **"Multi-Agent Orchestration"** | See ⚠️ **B** below | v4 | ⚠️ |

---

## Open honesty items

### ⚠️ A — "BM25" is not what Postgres full-text search does

**The problem.** `ts_rank` and `ts_rank_cd` are not BM25. No document-length normalisation, no IDF
saturation, different term-frequency handling. Anyone who has implemented retrieval knows this,
and "BM25" is a specific enough term that using it loosely reads as either imprecision or bluffing
— both worse than the simpler true claim.

**Resolution options** (full detail in `decisions.md` D-007):

1. Ship `ts_rank`, write **"lexical (Postgres FTS) + dense retrieval"**. Free and honest.
2. Implement real BM25 scoring in SQL. Most work, best story, only worth it if the ablation shows
   lexical scoring quality actually moves the number.
3. Add the `pg_search`/ParadeDB extension for genuine BM25 in Postgres.

**Blocking:** bullet 3 cannot be finalised until this is resolved. **Due:** end of v3.

### ⚠️ B — The title promises multi-agent; no bullet delivers it

**The problem.** The project is titled *"Multi-Agent Orchestration & RAG Runtime"*, but all three
bullets describe a **single**-agent runtime. Nothing mentions delegation, sub-agents, budget
slicing, or per-agent tool allowlists. A reader who notices will assume the title is inflated, and
that suspicion contaminates the bullets that *are* fully true.

**Resolution options:**

1. **Build it** (plan v4.6: `delegate` as a nested run, budget slicing, allowlists) and add a
   fourth bullet, or fold delegation into bullet 1. Preferred — the design already supports it,
   and nested-run delegation is a genuinely good interview topic.
2. **Retitle** to *"Durable Agent Runtime & Hybrid RAG"*, which is what the bullets actually
   describe and is a strong title on its own.

**Do not** leave the title claiming a capability no bullet supports. **Due:** before the CV is
sent anywhere.

### 🟨 C — Not one number in three bullets

**The problem.** Compare the QueueFair bullets — *20K+ concurrent SSE connections, p99 < 200ms,
3-node cluster, 100K virtual users, 225 → 18 queries (~92%)*. Every one of those is a measured
quantity, and they are why that project reads as engineering rather than as a tutorial follow-along.

The AgentForge bullets currently claim only capabilities. Side by side on one page, the second
project looks weaker than it is — not because it is smaller, but because it is unquantified.

**Where the numbers should go** (each is already an exit criterion in `plan.md`, so this costs no
extra work — only recording the result):

| Bullet | Number to earn | Source |
| --- | --- | --- |
| 1 | TTFT p95 (target < 1.5s), sustained concurrent runs | v2 exit, N1 |
| 2 | *"validated with N kill-9 chaos runs: zero duplicate tool executions, mean resume < Xs"* | v1 exit, N3/N5 |
| 3 | *"nDCG@10 0.XX → 0.YY over dense-only on a 50-question golden set, at +Nms p95"* | v3 ablation |

The bullet-2 number is the most valuable of the three, because **zero duplicate side effects under
kill-9** is a correctness claim, and correctness claims are rarer and harder to fake than
throughput ones.

### 🟨 D — Redis is in the tech list but does nothing in the bullets

Redis is named in the header stack and never appears in a bullet. It is doing real work — Redis
Streams for replayable SSE, idempotency lookups — and the *replayable* part (`Last-Event-ID` →
`XRANGE`, so a reconnecting client loses no tokens) is a genuinely good detail. Worth four words
in bullet 1: *"token streaming over SSE with replay-on-reconnect"*.

---

## Rule

**No number reaches this file, or my CV, until `eval-report.md` records the run that produced
it.** Not a plausible estimate, not a number from one lucky local run — the recorded run, with its
configuration next to it. `eval-report.md` is the evidence; this file is only the claim.
