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

---

## D-018 — Tool dependencies arrive by closure, not by a context parameter · 2026-09-19 · ACCEPTED

**Context.** `calculator` is a pure function and needs nothing. `now` has to read a clock, which
is IO, which `core/` cannot reach for. That is the first tool to test the boundary, and every
later tool — retrieval needs a `Retriever`, anything durable needs a store — has the same shape.

**Decision.** A tool that needs a dependency is built by a factory that closes over the port:
`clock_tool(clock: Clock) -> ToolSpec[NowInput]`. The handler signature stays
`(validated_input) -> str`.

**Why.** The alternative is a context parameter — `(input, ctx)` — where `ctx` carries the clock,
the run id, a store, and whatever the next tool needs. That makes every tool pay for the union of
every tool's dependencies, and the context object grows monotonically because nothing ever
removes a field from it. It also weakens the type: `ctx.retriever` is present for tools that do
not use it, so nothing states which dependencies a given tool actually has. The factory states it
in its own signature, checked by mypy.

The runtime facts a tool might seem to need — run id, step index — turn out not to be the tool's
business: the idempotency key is computed by the runtime from `(run_id, step_idx, tool_name,
args)` (D-004), and Celery dispatch is the runtime's job too. So the context object would mostly
carry things tools should not have.

**Rejected.** A context parameter on every handler (unstated dependencies, monotonic growth).
Module-level singletons for the clock and retriever (the global-configuration problem of D-013,
arriving through a different door, and it would put IO inside `core/`).

**Give up.** A tool needing five dependencies gets a five-argument factory. If that happens, the
factory's own arguments are the signal to reconsider — which is a visible signal, unlike a field
quietly added to a shared context.

**Revisit if** a tool genuinely needs per-invocation runtime state rather than per-registration
dependencies, which is the one case a closure cannot express.

---

## D-019 — Unknown block types pass through; an unknown stop_reason does not · 2026-09-19 · ACCEPTED

**Context.** The adapter meets two kinds of value it does not recognise: a content block
of a type we have never seen, and a `stop_reason` we have never seen. The consistent-looking
answer is to treat both the same way.

**Decision.** Unknown **blocks** are carried through verbatim as `OpaqueBlock`. An unknown
**stop reason** raises `LLMRequestError` and fails the run.

**Why.** They are opposite risks, so consistency would be the wrong goal.

A block we do not understand is one we do not act on — we only replay it, and replaying it
unchanged is exactly right. Failing on it would mean a new block type in a future API version
breaks every run, for a value that was never going to be read.

A `stop_reason` is the one field the loop branches on. Every safety property in the loop —
refusal handling, truncation handling, "is this an answer or a tool call" — hangs off it. A new
terminal condition silently falling into the `end_turn` branch would be treated as a finished
answer, which is a wrong result delivered confidently and with no error anywhere. Failing loudly
means someone adds the value to the `StopReason` literal *deliberately*, having decided what the
loop should do about it.

The general principle: be liberal in what you carry, strict in what you branch on.

**Rejected.** Fail on both (a future block type becomes an outage for no benefit). Pass through
both, defaulting an unknown stop reason to `end_turn` (silently converts a new terminal condition
into a confidently wrong answer — the worst available failure).

**Revisit** never as a principle. The literal itself is expected to change as the API grows, and
changing it is the point.

---

## D-020 — Statuses are varchar plus a Python enum, not a native Postgres enum · 2026-09-19 · ACCEPTED

**Context.** `runs.status`, `run_steps.stop_reason` and `runs.error_code` are all closed sets of
short strings. Postgres has a native `ENUM` type for exactly this, and it gives database-level
integrity.

**Decision.** Store them as `String(n)`. The closed set lives in the `StrEnum` in
`core/runtime/state.py`, and the store converts on the way in and out.

**Why.** The values are expected to grow — `NEEDS_REVIEW` arrives with the idempotency ledger,
new `ErrorCode` values arrive with every failure mode we learn about. Adding a value to a native
enum is `ALTER TYPE ... ADD VALUE`, which is a migration that takes a lock on the type and cannot
be rolled back in the same transaction. That turns "we found a new terminal condition" into a
deployment event.

The integrity we give up is smaller than it looks: nothing writes these columns except the store,
the store converts through the enum, and the enum is what the code branches on. A native enum
would be protecting the database from a writer that does not exist.

**Rejected.** Native `ENUM` (migration cost on every new value, for integrity against a writer we
do not have). A `CHECK` constraint listing the values (same migration cost, less type safety).

**Give up.** A hand-written `INSERT` could store a nonsense status. The mitigation is that
`RunStatus(run.status)` raises on read, so a bad value is caught at the boundary rather than
silently branched on.

**Revisit if** something outside this codebase starts writing these tables — a reporting job, a
second service — at which point the database becomes the only place the constraint can live.

---

## D-021 — The ledger is completed in its own transaction, not the step's · 2026-09-19 · ACCEPTED · refines D-004

**Context.** D-004 specified that a `tool_invocations` row is written `PENDING` before execution
and "completed in the same transaction as the step row". Implementing it forced the timeline to
be drawn properly, and that detail turns out to be the weaker of two options.

**Decision.** `complete()` commits on its own, immediately after the tool returns and **before**
the step is committed. D-004 stands in every other respect; only this detail is refined.

**Why.** Four points, three gaps:

    t0  ledger row PENDING committed
    t1  tool executes            <- the side effect, outside Postgres
    t2  ledger completed with the result
    t3  step row committed

A crash between **t2 and t3** is the common case — the tool finished, the next model call is
where the time goes, and the process can die anywhere in between. Completing the ledger at t2
closes that window *entirely*: the resumed run redoes the step, finds SUCCEEDED, and returns the
recorded result without executing again.

Deferring completion into the step transaction merges t2 into t3 and leaves that whole span
reporting PENDING — the ambiguous state. Every crash in it would then be resolved by the effect
class, so an `UNSAFE` tool that had already finished successfully would stop the run for human
review it did not need. Strictly more manual intervention, for no gain.

The window that remains, **t1 to t2**, is the one that genuinely cannot be closed: the side
effect is not in our database, so no transaction covers it. That is what the effect class is for,
and it is now the only such window.

**Rejected.** Completing inside the step transaction (D-004 as literally written — sends more
crashes down the ambiguous path). One transaction held open across t0-t3 (keeps a database
transaction open across a tool call and an LLM call, which can be minutes; connection exhaustion
under any real concurrency).

**Give up.** A ledger row can say SUCCEEDED for a step that is not committed. That is intentional,
and it is precisely the state a resume reads in order to avoid re-execution — the ledger records
*attempts*, the step table records *progress*, and this decision is about not conflating them.

**Revisit if** a tool ever needs its result to be atomically consistent with the step, which would
mean the result is itself the side effect — at which point it belongs in our database and in the
step transaction.

---

## D-022 — Exhausting a step's retries pauses the run; exhausting the run's ends it · 2026-09-19 · ACCEPTED

**Context.** D-009 fixed two retry bounds — per step and per run — but not what happens when
either is reached. The obvious answer is that both fail the run.

**Decision.** Per-step exhaustion leaves the run **PAUSED** and resumable, releasing the lease.
Per-run exhaustion fails it terminally with `STEP_FAILED`.

**Why.** The two bounds are reached for different reasons and only one of them says anything
durable about the run.

A step runs out of attempts because something upstream was unavailable *just then* — a rate
limit, a 503, a timeout. Marking that terminal means a five-minute provider blip permanently
destroys every run in flight, and a user who asked a question at the wrong moment gets nothing
back with no way to recover it. Pausing costs nothing and preserves every committed step.

A run runs out of retries because it has been failing repeatedly across steps and across
processes. That is durable evidence that something is actually wrong, and continuing to retry
spends money to keep discovering it.

This also gives the durable per-run counter a job it could not otherwise have. If step
exhaustion were terminal, the run counter would never be consulted twice; it is only meaningful
because a paused run can come back and must not arrive with a fresh allowance.

**Rejected.** Both terminal (a transient outage kills everything in flight). Both pausing (the
run counter then bounds nothing, and an automatic resumer would retry forever). Distinguishing by
exception type rather than by which bound was hit (the same 503 should pause early in a run and
fail late in one, and only the counter knows the difference).

**Give up.** A paused run needs something to resume it — the recovery scan, or an operator. Until
`POST /runs/{id}/resume` and the scan loop exist, a paused run sits still. That is the honest
state and it is visible in `runs.status`.

**Revisit if** we see runs pausing and resuming in a loop without progressing, which would mean
the run-level bound is too high rather than that the split is wrong.


---

## D-023 — A Celery task is one tool invocation, and the ledger is the rendezvous · 2026-09-22 · ACCEPTED

**Context.** `ExecutionMode.DURABLE` has existed on `ToolSpec` since v0.3 with nothing
consuming it. `architecture.md` §7 described two incompatible designs one sentence apart —
"the loop code does not branch on this — the registry does" (dispatch is invisible to the loop)
and "the run moves to `WAITING_TOOL`, and the worker that finishes it hands the run back" (the
loop stops and something else restarts it). The code could not be written until one won.

**Decision.** The Celery task is **one tool invocation**. The loop keeps the lease and waits.

1. The loop `lookup`s the idempotency key. **No row** → enqueue the task. **A terminal row** →
   replay it. **A `PENDING` row** → somebody is executing it; wait.
2. The **task** writes the ledger row, executes, and completes it — the same `begin`/`complete`
   protocol an inline tool uses, running in a different process.
3. The loop polls the ledger until the row is terminal or the step's deadline passes. There is no
   Celery result backend; **the rendezvous is a Postgres row**.
4. A tool still in flight at the deadline pauses the run with `TOOL_PENDING` instead of burning
   the step's retries. A later resume finds the result already recorded.

**Why.** Three forces, and this is the only shape that satisfies all of them.

*D-003 says Redis holds nothing we cannot lose.* A Celery result backend is durable run state in
Redis — a tool result that no longer exists after a `FLUSHALL`. Recording the result in the
ledger we already have keeps the invariant and costs nothing, because the ledger is *already* the
thing that must know the outcome to make replay work.

*The hand-back design pays an LLM call for every durable tool call, not just for crashes.* To
release the lease mid-step you must discard the assistant turn, because a `tool_use` block cannot
be persisted without its matching `tool_result` — the next request would be malformed. So the
re-drive re-issues the model call. At our budget (§7) that is a per-call tax on the common path,
and it is also a correctness risk: the second call may choose a *different* tool, orphaning the
result the first one is waiting on.

*The broker already guarantees delivery; duplicating that is how you get two of everything.* When
the loop finds a `PENDING` row it does **not** re-enqueue. `acks_late` plus
`task_reject_on_worker_lost` is what re-runs a task whose worker died, and the ledger is what
stops the redelivery becoming a second effect. Each layer does one job.

What DURABLE actually buys, stated precisely: **the tool is not running in the process that can
die.** An inline tool killed mid-execution leaves the unanswerable t1–t2 window and `effect_class`
has to resolve it. A durable tool survives its dispatcher — the run comes back and finds the
answer waiting. The window moves into the Celery task, where at-least-once redelivery reopens it,
which is why the task runs the same ledger protocol rather than trusting the queue.

**Rejected.** *A task per run* (`DURABLE` would then mean nothing, and a slow tool would have to
fit inside the step timeout and the lease TTL). *Hand-back via `WAITING_TOOL`* (above; also a
sixth run status and a second re-entry path that only executes after a crash). *Celery's result
backend as the rendezvous* (violates D-003). *A second wait budget for durable tools* (Q31 argues
the step is one budget; splitting it makes the configured number mean nothing).

**Give up.** Polling. The loop queries one indexed row every 100ms–2s while a durable tool runs;
a ten-minute tool is a few hundred cheap queries. `LISTEN`/`NOTIFY` removes them and is a known
upgrade, not a rewrite. We also give up the worker slot: the run's driver is blocked for the
tool's duration, which is fine while a run is sequential and stops being fine when a step
dispatches several durable tools at once.

There is one narrow duplicate-dispatch window: the loop enqueues, dies before the task inserts its
row, resumes, sees no row and enqueues again. Two tasks race the unique constraint, one executes
and one finds `PENDING` — resolved by `effect_class`, conservatively. It is milliseconds wide and
it fails in the safe direction.

**Revisit if** a step needs to dispatch several durable tools concurrently (the blocked driver
becomes the bottleneck and the hand-back design starts to earn its cost), or if polling shows up
in a latency measurement.

---

## D-024 — The Celery worker runs an async engine on its own event loop · 2026-09-22 · ACCEPTED

**Context.** CLAUDE.md §8 says Celery is not asyncio: workers use the sync engine and the sync
driver. But `ToolLedger` and every tool handler are `async`, and the task needs the ledger.

**Decision.** Each worker **process** creates one event loop and one asyncpg engine in
`worker_process_init`, and every task body runs on that loop via `run_until_complete`. No sync
`ToolLedger` is written.

**Why.** The §8 warning is about a specific bug — importing the API's async session into a task,
where the engine was built in another process (before `fork`) or bound to an event loop that is
no longer running. Building the engine *inside the forked child*, on a loop that lives as long as
the process, is not that bug: nothing is inherited across the fork and nothing outlives its loop.

The alternative is a second `ToolLedger` in sync SQLAlchemy. That is a duplicate of the most
subtle SQL in the project — insert-first-and-let-the-constraint-arbitrate, the `PENDING` read,
`resolve_pending` — in a file with no integration test of its own, kept in step with the async one
by hand. `loop.py` already refuses a second implementation of the loop for tests on exactly this
argument; the ledger deserves the same answer.

One loop per process, not one per task: `asyncio.run` per task would bind a new connection pool to
a loop that is then destroyed, so every task would pay a fresh TCP connect and the pool would
never be a pool.

**Rejected.** A sync `ToolLedger` (duplicate semantics, drift). `asyncio.run` per task (no pooling;
also the engine could not be created once). `--pool=solo` with an outer loop (throws away the
process isolation that is most of the point of dispatching at all).

**Give up.** `database_url_sync` and `psycopg` now have no consumer. They stay in the manifest
because a sync-only worker path is still plausible (a CPU-bound reranking task in v3 has no reason
to be async), and because deleting a setting is a bigger change than leaving one unread. If v3
lands without using them, they go.

**Revisit if** a task ever needs to run something that blocks the loop for a long time — that
wants a thread or a separate queue, not a different engine.
