# Decision Log — AgentForge

Append-only. Every non-obvious choice gets an entry: what we chose, what we rejected, why, and
what would make us revisit. Newest at the bottom. Never edit a past decision — supersede it with
a new entry that references the old ID, the way D-011 supersedes nothing yet but will one day.

This file is the source of *why*. `architecture.md` links here rather than restating, so an ADR
never exists in two places.

**Status key:** `ACCEPTED` · `OPEN` (decision not yet made) · `SUPERSEDED by D-nnn`

---

## D-001 — FastAPI, not async Django · 2026-08-20 · ACCEPTED

**Context.** QueueFair is async Django on ASGI, and that decision is logged there with real
reasoning. The lazy move is to repeat it for consistency.

**Decision.** FastAPI + Pydantic v2 for AgentForge.

**Why.** This project's edge is typed JSON in, SSE out, and a tool registry whose schemas are
generated from Pydantic models. That is Pydantic's home turf, and FastAPI is Pydantic with a
router attached. Django's advantages — ORM-integrated admin, mature sync ecosystem, batteries —
buy us nothing here, while its per-request middleware overhead and the sync/async adaptation
footgun cost us something real. Secondary but honest: one portfolio demonstrating both frameworks
is worth more than one demonstrating a single framework twice.

**Rejected.** Async Django (no upside here for real cost); Litestar/Starlette bare (FastAPI's
dependency injection and OpenAPI generation are worth the thin layer).

**Give up.** Django's admin for inspecting runs. We build one debug endpoint instead.

**Revisit if** we ever need a full user/permissions system, where Django's batteries would win.

---

## D-002 — Manual agent loop, not the SDK tool runner · 2026-08-20 · ACCEPTED

**Context.** The `anthropic` SDK ships `client.beta.messages.tool_runner`, which drives the
tool-calling loop automatically: define tools with `@beta_tool`, iterate the runner, done. It is
the recommended path for most applications, and it would delete a large amount of our code.

**Decision.** Hand-written loop in `core/runtime/loop.py`. The runner is not used.

**Why.** The runner owns the loop, and the loop is exactly where our requirements live. We need
to: commit a durable checkpoint between every step; enforce a per-step timeout distinct from the
run timeout; route some tool calls to Celery instead of executing them in-process; and resume a
half-finished run from Postgres on a different machine. None of those are hooks the runner
exposes — it is a helper for the case where the loop is uninteresting, and here the loop *is* the
project. The SDK docs themselves say to drop to a manual loop when you need control the runner
does not expose.

Secondary: the runner is beta, and this is a portfolio project where "I wrote the ReAct loop" is
a claim I want to be able to make and defend line by line.

**Rejected.** `tool_runner` (above). LangChain/LlamaIndex — importing an orchestration framework
into a project whose subject is orchestration deletes the project.

**Give up.** Roughly 150 lines we now maintain, and the SDK's automatic handling of `pause_turn`
for server-side tools, which we must handle ourselves if we ever enable them.

**Revisit if** the runner gains per-step interception hooks that cover checkpointing.

---

## D-003 — Postgres is the source of truth; Redis is transport · 2026-08-20 · ACCEPTED

**Context.** QueueFair put *all* state in Redis and had no database at all — a decision logged
there and defensible there. The instinct is to carry it over.

**Decision.** Postgres holds runs, steps, messages and the tool ledger. Redis holds event
streams, idempotency lookups and cache. Flushing Redis loses live streams and nothing else.

**Why.** The requirements inverted. QueueFair's state was ephemeral and high-churn (a queue
position is worthless sixty seconds later), which is Redis's shape. A run is an execution record
with an audit trail that must survive a crash, be queried after the fact, and support
`UNIQUE(run_id, idx)` and foreign keys. That is a database, and pretending otherwise to reuse the
previous project's answer would be cargo-culting.

**Rejected.** Redis-as-truth with AOF persistence — durable-ish, but no transactions across the
step-and-ledger write, no relational integrity, and recovery semantics we would be hand-rolling.
Event-sourcing to a log — the append-only steps table already gives us the replay property
without a second system.

**Give up.** A Postgres write on the hot path of every step (~ms; N2 caps it at 15ms p95) and a
hard dependency: Postgres down means runs stop.

**Revisit if** step commit latency ever shows up in a p99 breakdown — measure before believing it.

---

## D-004 — Idempotency ledger with declared effect classes · 2026-08-20 · ACCEPTED

**Context.** A crash between "the tool ran" and "we recorded that it ran" is unavoidable: the
side effect is not in our database, so no transaction covers both. On resume we genuinely cannot
tell whether it happened.

**Decision.** A `tool_invocations` ledger keyed by `hash(run_id, step_idx, tool_name, args)`,
written `PENDING` *before* execution and completed in the same transaction as the step row. Every
tool declares an `effect_class`: `READ_ONLY`, `IDEMPOTENT_WRITE`, or `UNSAFE`. A ledger row found
`PENDING` on resume is resolved by that class — re-execute, re-execute (downstream dedupes), or
stop the run for review.

**Why.** Exactly-once is not achievable; at-least-once with effectively-once outcomes is. The
only component that can answer "is replaying this safe?" is the tool author, so the design forces
them to answer it once, at definition time, in a required field — rather than leaving the runtime
to guess at 3am.

**Rejected.** Blind retry (silently double-charges, and would only be discovered in production).
Never retry (turns every crash into a dead run, throwing away the durability we just built).
Two-phase commit / distributed transactions (the external services do not participate, so it is
not even available). Saga compensation (correct and much larger; the effect-class gate is the 80%
that fits this project).

**Give up.** `UNSAFE` tools do not auto-recover — a human resolves them. That is the intended
behaviour, not a limitation.

**Revisit if** we add a tool that genuinely needs compensating transactions.

---

## D-005 — Redis Streams for run events, not pub/sub · 2026-08-20 · ACCEPTED

**Context.** QueueFair fanned out over Redis pub/sub and that worked, because a waiter who
reconnects only needs the *current* position — stale messages are worthless. Here, a client that
disconnects for three seconds mid-answer must not lose the tokens emitted in that gap.

**Decision.** One Redis Stream per run. The SSE `id:` field is the stream entry ID verbatim, so a
reconnect with `Last-Event-ID` becomes `XRANGE` from that ID, then tail live. `MAXLEN` + TTL bound
memory and define how long a client may be away and still catch up.

**Why.** Pub/sub has no memory; a subscriber that is not connected at publish time never learns
the message existed. Streams are a log with monotonic IDs, which is precisely the replay
primitive SSE's `Last-Event-ID` was designed around. Using the stream ID directly means no
sequence numbers of our own and no translation layer to get wrong.

**Rejected.** Pub/sub (no replay). WebSockets (bidirectional, and we only ever push — SSE
reconnects for free and survives proxies better). Long-polling (worse on every axis here).
Postgres `LISTEN/NOTIFY` (no replay either, and it puts fan-out load on the truth store).

**Give up.** Memory per stream, and — the real cost — one blocking `XREAD` per SSE connection, so
Redis connections scale with clients. Fine into the low hundreds. The fix (sharded streams,
`hash(run_id) % K`, one reader task per process fanning out to `asyncio.Queue`s — the QueueFair
shape) sits behind the `EventBus` port and gets built when a load test demands it, not before.

**Revisit when** a load test shows SSE connection count as the binding constraint.

---

## D-006 — pgvector in the application database · 2026-08-20 · ACCEPTED

**Context.** Hybrid retrieval needs a vector index and a lexical index. The default industry
answer is a dedicated vector database.

**Decision.** One Postgres. pgvector + HNSW for dense, `tsvector` + GIN for lexical, in the same
database as the run tables.

**Why.** At our corpus size (target 100k chunks) a specialised store buys nothing measurable, and
costs an operational dependency, a second consistency boundary, and a network hop per query. In
one database, a chunk's text, its embedding, its lexical index and its owning document are one
row with one transaction — no dual-write problem, no "the vector store says this chunk exists and
Postgres disagrees". Being able to explain *why one database is sufficient* is a stronger
interview answer than having run Pinecone.

**Rejected.** Pinecone (hosted cost, and the dual-write problem for free). Qdrant/Weaviate (a
second stateful service to operate for a benefit we cannot yet measure). FAISS in-process (no
persistence, no filtering, no concurrent writers).

**Give up.** Filtered vector search is a known weak spot — a `WHERE tenant_id` can push the
planner off the HNSW index. Mitigated by `EXPLAIN ANALYZE` on every retrieval query, and by
partial indexes if it bites.

**Revisit if** the corpus passes ~1M chunks or filtered-search latency becomes the top cost.

---

## D-007 — Lexical scoring: `ts_rank` or real BM25? · 2026-08-20 · **OPEN**

**Context.** The resume bullet says "BM25 + dense retrieval merged with reciprocal rank fusion".
Postgres full-text search does **not** implement BM25. `ts_rank`/`ts_rank_cd` are different
functions: no document-length normalisation, no IDF saturation, and term frequency handled
differently. An interviewer who knows retrieval will ask, and "close enough" is a bad answer.

**Options.**

1. **Ship `ts_rank`, change the resume wording** to "lexical (Postgres FTS) + dense". Free,
   honest, slightly less impressive.
2. **Implement BM25 in SQL** over `ts_stat`/term-frequency data. Real BM25, genuinely
   interesting to explain, and the most work — plus a performance question at scale.
3. **Add the `pg_search` / ParadeDB extension**, which implements real BM25 in Postgres. Accurate
   claim, small operational cost, and a Docker image change.

**Leaning.** (1) until v3 measurements exist, then (2) if the ablation shows lexical scoring
quality actually matters for our corpus — because implementing BM25 is only worth it if the number
moves.

**Decision required by** the end of v3 (retrieval), before any resume claim is finalised. Tracked
in `resume-claims.md` as a blocking honesty item.

---

## D-008 — Local embedding + reranking models · 2026-08-20 · ACCEPTED

**Context.** Embeddings can come from a hosted API or a local model. Anthropic does not offer an
embeddings endpoint, so a hosted option means adding a second vendor.

**Decision.** Local, CPU, via `sentence-transformers`: `bge-small-en-v1.5` (384-dim) for
embeddings, `bge-reranker-base` for cross-encoding. Both behind `Embedder` / `Reranker` ports.

**Why.** Three reasons in priority order. **Cost:** re-embedding the corpus is free, so we can
re-chunk and re-index as often as experiments demand — and with a paid API, "let's try a different
chunk size" quietly becomes a cost decision, which is exactly the wrong incentive on a project
whose point is measurement. **Reproducibility:** CI runs the eval suite without a network or a
key. **Honesty:** a 384-dim model is weaker than a frontier embedding API, and the eval harness
will show by how much — which is a more interesting result than not knowing.

**Rejected.** Hosted embedding APIs (per-query cost, second vendor, network in the hot path).
LLM-based reranking (10–100× the cost of a cross-encoder for the same job).

**Give up.** ~500MB of torch in the image, slow cold start, and CPU forward passes that dominate
retrieval p99 (N4). The ports mean swapping to a hosted model is a one-file change if the eval
says it is worth it.

**Revisit if** eval shows embedding quality is the binding constraint on answer quality.

---

## D-009 — Retries bounded at two levels, counted deliberately · 2026-08-20 · ACCEPTED

**Context.** The SDK retries internally (default 2). Adding per-step and per-run retries without
thinking gives 2 × 3 × 3 = 18 attempts for one user action, which is how a rate limit becomes an
outage.

**Decision.** Two application layers: bounded retries per step (recorded in `run_steps.attempt`,
so every retry is visible in the audit log) and a cap on total retries per run. The SDK's internal
retry stays at its default and is *counted* as the third layer when reasoning about worst-case
attempts. Retry state is durable, so a resumed run does not get a fresh allowance.

**Why.** Retries that live in a loop counter vanish on crash and are invisible in production.
Retries that live in a row can be queried, alerted on, and explained.

**Rejected.** SDK retries only (no control over what counts as retryable for *our* semantics).
Unbounded retry with backoff (unbounded cost, and a runaway loop is this project's most expensive
failure mode).

**Revisit if** we see step failures that succeed on attempt 3+ regularly — that would mean the
bound is too tight, or something upstream needs fixing instead.

---

## D-010 — Reciprocal Rank Fusion, not weighted score blending · 2026-08-20 · ACCEPTED

**Context.** Two ranked candidate lists must become one.

**Decision.** RRF: `score(d) = Σ_i 1/(k + rank_i(d))`, k=60 (the value from the original Cormack
et al. paper; it damps the influence of top ranks enough that one retriever cannot dominate).

**Why.** Cosine similarity and `ts_rank` are not on a comparable scale, and no normalisation makes
them principled — min-max is corpus-dependent and z-score assumes a distribution neither has. Any
weighted blend introduces a tunable that will be fitted to whatever set we happen to test on. RRF
discards the scores and uses only ranks, which removes the tunable most likely to be silently
overfit. It is also ~10 lines, so it is learning surface, not a dependency.

**Rejected.** Weighted score normalisation (magic weight, overfits). Learning-to-rank (needs
training data we do not have, and this is not an ML project).

**Give up.** Genuine score *margins* — RRF cannot tell "rank 1 by a mile" from "rank 1 by a hair".
The cross-encoder immediately downstream re-introduces real scores, so the loss is bounded.

**Revisit if** the eval ablation shows fusion is the weak link, in which case k is the first knob
and it gets tuned on the held-out slice only.

---

## D-011 — Prompts as versioned files, not string literals · 2026-08-20 · ACCEPTED

**Context.** System prompts and tool descriptions started life as constants in module scope. It
is the obvious thing and it is wrong.

**Decision.** `app/prompts/<name>.v<N>.txt`, loaded by name and version. The version is recorded
on every run row and on every eval result.

**Why.** A prompt change alters system behaviour exactly the way an algorithm change does, so it
must be diffable in review, attributable in git blame, and — critically — recorded next to eval
numbers. Without the version on the eval result, a quality regression cannot be traced to the
prompt edit that caused it, and the eval harness stops being a regression gate.

Second-order: prompt text is a byte-exact prompt-cache prefix. Keeping it in a file makes
accidental invalidation (an f-string with a timestamp in it) structurally harder.

**Rejected.** Constants in code (invisible in eval results). A database table (adds a lookup and a
migration to change a sentence; git already does versioning well).

---

## D-012 — Durability before retrieval in the build order · 2026-08-20 · ACCEPTED

**Context.** RAG is the demo-able half and the tempting place to start. Durability is the
engineering half.

**Decision.** v1 is durability and resumption. RAG does not start until v3.

**Why.** Retrofitting checkpointing into a working loop means rewriting the loop, whereas adding
retrieval to a durable runtime is adding one tool. The dependency runs one way. And honestly: if
the fun part gets built first, the hard part never gets built, and the project becomes another
RAG demo with an agent loop bolted on — which is the thing this project exists not to be.

**Rejected.** RAG-first (demo sooner, rewrite later). Both in parallel (nothing finished, on a
solo project).

**Revisit** never. If this is being reopened, it is procrastination wearing an architecture
costume.

---

## D-013 — Settings by injection, not a module-level singleton · 2026-09-19 · ACCEPTED

**Context.** Every module needs configuration, and the obvious Python idiom is
`settings = Settings()` at module scope in `settings.py`, imported from everywhere.

**Decision.** `Settings` is a **frozen** pydantic-settings model. Process entry points call
`get_settings()`, cached with `lru_cache(maxsize=1)`. `create_app()` takes an optional `Settings`
and stores it on `app.state`; handlers read `request.app.state.settings`. Tests construct
`Settings(_env_file=None, ...)` directly and never call `get_settings()`.

**Why.** A module-level singleton is built at *import* time, so importing any module that touches
config requires a valid environment — a missing variable becomes an `ImportError` during pytest
collection instead of a clear error where it is used. It also makes per-test configuration
impossible without monkeypatching a module global, which then leaks between tests. The factory
plus a frozen model lets the unit suite vary configuration with no environment at all, which is
precisely what the v0 exit criterion ("green with no network, no Docker") demands. Frozen, because
configuration that can mutate at runtime cannot be reconstructed from a log line afterwards.

**Rejected.** Module-level singleton (import-time environment dependency; untestable).
`Depends(get_settings)` on every route (works for HTTP, but the cache is still a global that tests
must override via `dependency_overrides`, and it does nothing for the Celery workers, which are
not FastAPI). Passing individual values instead of the object (every new setting rewrites every
signature in the call chain).

**Give up.** Two ways to reach configuration. The rule that keeps it honest: **only entry points
call `get_settings()`**; everything downstream receives a `Settings` it was given.

**Revisit if** per-tenant or per-run configuration overrides appear — a process-wide singleton
would then be wrong in a second, worse way.

---

## D-014 — Core owns its message types, and only interpreted blocks get fields · 2026-09-19 · ACCEPTED

**Context.** The loop needs a vocabulary for messages, blocks and responses. The `anthropic`
SDK already ships exact types for all of it, and its own guidance is explicit: do not redefine
SDK data structures, you lose type safety and duplicate what exists.

**Decision.** `core/runtime/messages.py` defines AgentForge's own types. Only the three block
kinds the loop actually interprets — `text`, `tool_use`, `tool_result` — have fields. Everything
else (thinking, redacted_thinking, server tool blocks, compaction, anything a future API version
adds) is an `OpaqueBlock` with `extra="allow"`, carried through untouched. `adapters/llm/`
translates in both directions.

**Why.** Two forces pull opposite ways and this resolves both.

The SDK's advice is correct for an application that calls the model. It is wrong here: `core/`
importing `anthropic` means the unit suite depends on the provider SDK, `FakeLLM` has to
construct real SDK objects to stand in, and the boundary that CLAUDE.md §5 exists to protect —
the one an interviewer asks about first — is gone.

But the naive form of "define your own types" is worse than either option. Parsing a thinking
block into our own fields and re-serialising it is *precisely* how the byte-identical round trip
in CLAUDE.md §8 breaks, and it breaks silently, taking the prompt cache with it — one bug, two
symptoms, neither of which raises. Typing only what we interpret means the blocks we must not
mangle have no parsing step to be mangled by. It also makes replay forward-compatible: a block
type introduced after this code was written round-trips unharmed, which is the same
ignore-what-you-do-not-understand rule `plan.md` §2.3 already applies to SSE events.

**Rejected.** SDK types in `core/` (couples the loop to a vendor and puts the provider SDK in the
unit suite's dependency set). Fully typed models for every block type (every new block type
becomes a breaking change, and thinking blocks get mangled — the worst of both). Everything as
`dict[str, Any]` (no typing where the loop genuinely needs it; tool dispatch degrades to
string-keyed lookups over unvalidated data).

**Give up.** A translation layer in the adapter, and tests that have to keep pace with the real
wire shape. Mitigated by the opt-in live smoke test (v0.8), which asserts the translation against
an actual response rather than our memory of one.

**Revisit if** the translation layer passes roughly 100 lines — that would mean we are
re-implementing the SDK rather than adapting it.

---

## D-015 — Ports land with the phase that implements them · 2026-09-19 · ACCEPTED

**Context.** CLAUDE.md §5 lists seven Protocols in `core/ports.py`: `LLMClient`, `Retriever`,
`Reranker`, `RunStore`, `EventBus`, `TaskQueue`, `Clock`. The natural reading is to write all
seven up front, since ports are supposed to come before implementations.

**Decision.** Write each port in the phase that first implements it. v0 ships `Clock` and
`LLMClient`. `RunStore` arrives in v1 alongside the tables, `EventBus` in v2, `Retriever` and
`Reranker` in v3.

**Why.** "Port before implementation" means before the *adapter*, not eight weeks before anyone
knows what the thing does. `RunStore`'s signature depends on the step and message tables that
task 1.1 defines; guessing it now produces an interface that is subtly wrong and — worse —
written down, so the v1 adapter gets built to match the guess instead of the requirement. An
absent port is a known gap; a wrong port is a silent one.

**Rejected.** All seven now (speculative design, and the file reads as architecture theatre).
No ports file until several adapters exist (loses the ordering that makes dependency inversion
real rather than retrofitted).

**Revisit** never as a principle, but note the failure mode it trades for: two ports that should
have shared a shape are designed independently. The check is a read of `ports.py` as a whole at
the start of each phase.

---

## D-016 — Tool schemas are emitted sorted by name · 2026-09-19 · ACCEPTED

**Context.** `ToolRegistry.schemas()` has to return the tools in *some* order. Insertion order is
the obvious choice and costs nothing to implement.

**Decision.** Sort by name. The same set of tools always produces the same bytes, regardless of
the order they were registered in.

**Why.** The `tools` array renders ahead of `system` and `messages`, so it sits at the very front
of the prompt-cache prefix. Caching is a prefix match: change a byte anywhere in the prefix and
everything after it is invalidated. With insertion order, the array's order depends on import
order — which changes silently when someone moves a registration during a refactor, or when two
processes build their registries along different code paths. The result is two workers that never
hit each other's cache, on every step of every run. Nothing fails; the bill just goes up, and
CLAUDE.md §7 is the constraint that notices.

Sorting removes the variable entirely. Adding or removing a tool still invalidates the prefix,
but that is a real change to what the model is being told, not an accident of import order.

**Rejected.** Insertion order (silently non-deterministic across processes). An explicit
`order` field per tool (a knob to get wrong, for no benefit over alphabetical).

**Give up.** The tool list cannot be ordered to put the most important tool first, if that ever
turns out to matter for model behaviour. No evidence it does; revisit with a measurement.

**Revisit if** an eval shows tool ordering measurably affects selection quality, in which case
the order becomes a deliberate, recorded part of the prompt rather than an accident either way.

---

## D-017 — Tools are registered explicitly, not by decorator · 2026-09-19 · ACCEPTED

**Context.** The ergonomic way to define a tool is a decorator over an async function that infers
the input model from the type hint and self-registers on import — which is roughly what the SDK's
own `@beta_tool` does.

**Decision.** Construct a `ToolSpec` and call `registry.register(spec)`.

**Why.** Two fields decide how the runtime behaves when a tool is interrupted mid-flight:
`effect_class` and `execution`. A decorator that infers most of a tool from its signature creates
pressure to give those a default too, and `effect_class` defaulting to anything is precisely the
failure D-004 exists to prevent. Explicit construction makes both visible at the definition site.

Self-registration on import is the second problem: it means the tool set depends on which modules
have been imported, which contradicts D-016's whole purpose and makes a per-agent allowlist (v4)
fight the framework instead of using it.

**Rejected.** A `@tool` decorator with self-registration (import-order-dependent tool set, and
pressure toward a default on the one field that must not have one). A decorator that returns a
`ToolSpec` without registering (defensible, mostly cosmetic — revisit if the definitions get
noisy).

**Revisit if** tool definitions become repetitive enough that the boilerplate hides the two fields
that matter, at which point the decorator must still require them positionally.

