# AgentForge — Architecture

**Status:** draft, pre-v0. **Owner:** Praneeth Simon Katta.
**Governs:** design. For *what the system does*, see `product-spec.md`. For *the wire format and
build order*, see `plan.md`. For *why we chose X over Y*, see `decisions.md` — this document
links to decision IDs rather than restating them.

---

## 1. The problem

An agent is a loop: ask the model, it asks for a tool, run the tool, give it the result, repeat
until it answers. That loop is twenty lines of Python, and every tutorial stops there.

It stops there because everything difficult starts one line later:

1. **The loop is long-lived.** A five-step run with a slow tool takes minutes. HTTP requests do
   not take minutes, and processes get restarted mid-deploy.
2. **The loop has side effects.** Steps are not pure. If step 4 charged a card and the process
   died before step 5, "just retry the run" is a bug with a financial cost.
3. **The loop is opaque.** The user sees a spinner for ninety seconds. Something has to stream
   partial output, and survive the user's wifi dropping for three of those seconds.
4. **The loop is only as good as its retrieval.** For a question-answering agent, nearly all
   answer quality is retrieval quality — and retrieval quality is invisible without measurement.

**AgentForge is the twenty lines plus the answers to those four problems.** The runtime is the
subject; the RAG corpus is what gives it something worth doing.

### Requirements

**Functional**

| ID | Requirement |
| --- | --- |
| F1 | Execute a multi-step tool-calling (ReAct) loop against a typed tool registry. |
| F2 | Stream tokens and lifecycle events to the caller as they are produced. |
| F3 | Survive process death: a crashed run resumes from its last completed step. |
| F4 | Execute long-running tools out-of-band without holding the request open. |
| F5 | Answer questions over an ingested corpus with citations to source chunks. |
| F6 | Let a supervisor agent delegate sub-tasks to sub-agents with their own budgets. |
| F7 | Measure retrieval and answer quality against a golden set, in CI. |

**Non-functional** — targets, not measurements. Every one of these is a hypothesis until
`eval-report.md` records the run that confirms it. (Rule 7.)

| ID | Target | Why this number |
| --- | --- | --- |
| N1 | Time-to-first-token p95 < 1.5s | Below ~2s a user reads it as "working", above it as "hung". |
| N2 | Step checkpoint overhead < 15ms p95 | Durability must cost less than 1% of a ~2s LLM step, or it will be argued away. |
| N3 | Resume after crash < 2s | Must feel like a hiccup, not an outage. |
| N4 | Retrieval p95 < 400ms at 100k chunks | The rerank stage dominates this; it is the number to defend. |
| N5 | Zero duplicate side effects under kill-9 | Binary, not statistical. The headline correctness claim. |
| N6 | Unit suite green with no network, no DB, no weights | If this breaks, the hexagonal boundary has rotted. |

**Explicit non-goals:** multi-tenancy beyond a `tenant_id` column, human-in-the-loop approval UI,
a second LLM provider, training or fine-tuning anything, horizontal scale past one box. Each is a
paragraph in this document rather than code.

---

## 2. System context

```
                    ┌──────────────┐
                    │   client     │  browser / curl / eval harness
                    └──────┬───────┘
              POST /v1/runs│  GET /v1/runs/{id}/events  (SSE, Last-Event-ID)
                    ┌──────▼─────────────────────────────────────┐
                    │            FastAPI  (async, stateless)     │
                    │  api/routes ──► core/runtime ──► ports     │
                    └───┬────────────┬───────────────┬───────────┘
                        │            │               │
          checkpoints   │            │ events        │ dispatch
          (source of    │            │ (transport)   │ (durable tools)
           truth)       │            │               │
                 ┌──────▼─────┐ ┌────▼─────┐  ┌──────▼──────┐
                 │ PostgreSQL │ │  Redis   │  │   Celery    │
                 │            │ │          │  │   workers   │
                 │ runs       │ │ streams  │  └──────┬──────┘
                 │ run_steps  │ │ idemp.   │         │
                 │ messages   │ │ cache    │         │ same DB,
                 │ tool_invoc │ └──────────┘         │ sync driver
                 │ documents  │◄──────────────────────┘
                 │ chunks     │
                 │  + pgvector│         ┌──────────────────┐
                 │  + tsvector│         │  Anthropic API   │
                 └────────────┘         │  claude-opus-5   │
                                        └──────────────────┘
```

**The load-bearing split:** Postgres is the *source of truth*; Redis is *transport and cache*.
Flush Redis and you lose live SSE streams and some warm cache — no run state, no history, no
correctness. That single sentence is the durability story, and it is the inverse of QueueFair,
where Redis held everything and there was no database at all. (D-003.)

---

## 3. Execution model: a run is a state machine, not a request

The central design commitment: **an agent run is a job, not an HTTP request.** `POST /v1/runs`
returns `202` with a `run_id` in single-digit milliseconds. Everything after that is the runtime
advancing a persisted state machine, and the client is an *observer* over SSE, not a participant.

This is what makes F2, F3 and F4 possible at once. If the run lived inside the request, a dropped
connection would kill it, a slow tool would block a worker, and resumption would have nowhere to
resume from.

```
                    ┌─────────┐
                    │ QUEUED  │  row committed, no lease yet
                    └────┬────┘
                         │ worker claims lease
                    ┌────▼────┐
        ┌──────────►│ RUNNING │◄──────────┐
        │           └────┬────┘           │
        │                │                │ resume (lease expired
   tool result           │                │  or explicit POST)
   arrives          ┌────┴─────┐          │
        │           │          │          │
   ┌────┴───────┐   │     ┌────▼──────────┴┐
   │WAITING_TOOL│◄──┘     │    PAUSED      │
   └────────────┘         └────────────────┘
        │                          ▲
        │  terminal:               │ crash / SIGKILL / lease expiry
        ▼                          │
   ┌───────────┐  ┌────────┐  ┌────┴──────┐  ┌───────────┐
   │ COMPLETED │  │ FAILED │  │ CANCELLED │  │ BUDGET_   │
   └───────────┘  └────────┘  └───────────┘  │ EXCEEDED  │
                                             └───────────┘
```

`BUDGET_EXCEEDED` is a first-class terminal state, not a flavour of `FAILED`. Running out of
steps or tokens is an *expected* outcome of an open-ended loop, and the operator response
(raise the cap, or accept it) is different from the response to a crash. Collapsing them hides
the most common real-world ending. It is also the cost guardrail from CLAUDE.md §7, made
structural.

**Where this diagram is ahead of the code.** `WAITING_TOOL` was removed by D-023 — a durable tool
no longer changes the run's status, so the state is unreachable by construction. `CANCELLED` and
`BUDGET_EXCEEDED` are still drawn here as statuses but are implemented as `ErrorCode` values on a
FAILED run; that divergence predates v1.7 and has not been argued either way yet.
`core/runtime/state.py` is the source of truth for which statuses exist.

**Progress is monotonic.** `run_steps` is append-only with `UNIQUE(run_id, idx)`; a step is never
updated in place. Recovery is therefore "read the committed steps, rebuild the message list,
continue at `max(idx)+1`" — no diffing, no reconciliation, and the table doubles as the audit log
that makes the whole run explainable after the fact.

---

## 4. Data model

Only the load-bearing columns are shown.

```sql
runs
  id              uuid pk
  parent_run_id   uuid null            -- set for delegated sub-agent runs (§8)
  agent_name      text                 -- which roster entry: prompt + tool allowlist
  status          run_status           -- the enum in §3
  input           jsonb
  output          jsonb null
  error           jsonb null
  step_count      int  default 0
  tokens_in       int  default 0       -- budget enforcement reads these
  tokens_out      int  default 0
  token_budget    int                  -- snapshot at creation; a sub-agent gets a SLICE
  idempotency_key text unique null     -- client-supplied: retrying POST /runs is safe
  lease_owner     text null            -- worker identity holding this run (§6)
  lease_expires_at timestamptz null
  created_at, updated_at timestamptz

run_steps                              -- APPEND ONLY. the checkpoint. the audit log.
  id          uuid pk
  run_id      uuid fk
  idx         int                      -- UNIQUE(run_id, idx): the whole recovery contract
  kind        step_kind                -- LLM_CALL | TOOL_CALL | DELEGATE | FINAL
  status      step_status              -- OK | ERROR | TIMEOUT
  attempt     int  default 0           -- bounded retries land here, visibly
  tool_name   text null
  request     jsonb                    -- what we sent / the tool input
  response    jsonb                    -- what came back / the tool output
  tokens_in, tokens_out int
  started_at, ended_at timestamptz

run_messages                           -- APPEND ONLY. the exact API transcript.
  run_id   uuid fk
  seq      int                         -- UNIQUE(run_id, seq)
  role     text                        -- user | assistant
  content  jsonb                       -- raw content blocks, byte-preserved (see below)

tool_invocations                       -- the exactly-once ledger (§6)
  id              uuid pk
  run_id          uuid fk
  idempotency_key text UNIQUE          -- hash(run_id, step_idx, tool_name, args)
  effect_class    text                 -- READ_ONLY | IDEMPOTENT_WRITE | UNSAFE
  status          text                 -- PENDING | DONE | FAILED
  result          jsonb null
  created_at, completed_at timestamptz

documents
  id, tenant_id, uri, title, sha256 unique, ingested_at

chunks
  id           uuid pk
  document_id  uuid fk
  ordinal      int                     -- position in doc: lets a citation say "para 4"
  text         text
  token_count  int
  embedding    vector(384)             -- pgvector, HNSW index
  tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
                                       -- GIN index. generated, so it can never drift from text.
```

Two notes that matter more than they look:

**`run_messages.content` stores raw content blocks, not flattened text.** Replay must reconstruct
the exact `messages` array we sent, including `tool_use` and `thinking` blocks. Thinking blocks
have to be echoed back unchanged on the same model, and any byte drift also breaks the prompt
cache — so "store the text and rebuild the blocks" is a bug that would show up as a mysterious
cost increase weeks later. (Trap, CLAUDE.md §8.)

**`chunks.tsv` is a generated column**, not one the application writes. If the application wrote
it, a code path that updates `text` without updating `tsv` would silently corrupt lexical search
in a way no test would catch. Let the database maintain the invariant.

---

## 5. The agent loop

`core/runtime/loop.py` is pure: it takes ports, not clients, and never imports SQLAlchemy, redis
or `anthropic`. That is what lets the entire loop — retries, budgets, resumption — be unit-tested
against a scripted `FakeLLM` with no Docker running. (N6.)

```
resume_or_start(run_id):
    state ← store.load(run_id)                # replays committed steps into messages[]
    claim_lease(run_id, ttl=90s)              # §6; SKIP LOCKED, not check-then-act

    while True:
        policy.check(state)                   # raises on step cap / token budget / cancel flag

        # ---- LLM step ------------------------------------------------------
        with timeout(STEP_TIMEOUT):
            stream ← llm.stream(messages, tools=registry.schemas(), effort=...)
            for delta in stream:              # tokens → Redis, never → Postgres
                events.publish(run_id, TokenEvent(delta))
            reply ← stream.final_message()

        check reply.stop_reason               # refusal / max_tokens BEFORE reading content
        store.commit_step(LLM_CALL, reply)    # ← durable. crash here loses nothing.

        if reply.stop_reason == "end_turn":
            store.finish(COMPLETED); return

        # ---- Tool step -----------------------------------------------------
        calls ← [b for b in reply.content if b.type == "tool_use"]
        results ← await gather(execute(c) for c in calls)   # parallel, bounded
        store.commit_step(TOOL_CALL, results)
        messages.append(single user message containing ALL tool_result blocks)
        renew_lease()
```

Five things in that sketch are deliberate and each is a question I expect to be asked:

1. **Tokens stream to Redis; only steps go to Postgres.** One row per step, not per token —
   otherwise a two-second answer becomes a two-hundred-write transaction storm.
2. **`stop_reason` is checked before `content` is read.** `refusal` is an HTTP 200 with no text
   block; reading content first turns a handled outcome into a `StopIteration` crash.
3. **All tool results go back in one user message.** Splitting them degrades parallel tool calling
   silently — no error, just worse behaviour over time.
4. **The step commits *after* the LLM call and *after* the tool batch,** which is what creates the
   crash window §6 exists to handle.
5. **The lease is renewed inside the loop.** A long tool must not let another worker conclude the
   run is abandoned and start a second copy of it.

### Per-step timeouts and bounded retries

Timeouts are **per step**, not per run: a run legitimately takes minutes, while a single step
hanging means a stuck socket. Retries are bounded at two levels — per step (`attempt` on the row,
so a retry is *visible* in the audit log rather than hidden in a loop counter) and per run in
total. The SDK also retries internally by default; three layers of invisible retry is how a rate
limit becomes an outage, so the layers are counted deliberately. (D-009.)

---

## 6. Durability and exactly-once effects

This is the part of the project worth interviewing about, so it gets stated precisely.

**Exactly-once execution is impossible.** A process can die in the gap between "the side effect
happened" and "we recorded that it happened", and no amount of transaction cleverness closes that
gap, because the side effect is not in our database. What is achievable is **at-least-once
delivery with effectively-once outcomes**, and only for tools that let us dedupe.

The timeline, which is the thing to draw on a whiteboard:

```
t0 ─── claim lease ──────────────────────────────────── run is mine for 90s
t1 ─── INSERT tool_invocations(key, PENDING)  ══ COMMIT ══►  intent is durable
t2 ─── tool executes                          ◄── THE SIDE EFFECT HAPPENS HERE
t3 ─── UPDATE invocation → DONE, result
       INSERT run_steps(idx=N)                ══ COMMIT ══►  one transaction
t4 ─── continue
```

Recovery reads the ledger and finds exactly one of three situations:

| State found on resume | What it means | What we do |
| --- | --- | --- |
| No row for this key | Crash before t1. The tool never ran. | Execute normally. |
| Row `DONE` with a result | Crash after t3. | Return the stored result. Do **not** re-execute. |
| Row `PENDING` | Crash between t1 and t3. **We cannot tell whether the effect happened.** | Depends on the tool's declared effect class. |

That third row is the honest answer, and it is why **every tool declares an effect class** in the
registry:

- **`READ_ONLY`** — retrieval, search, calculation. Re-executing is free. Just run it again.
- **`IDEMPOTENT_WRITE`** — carries a natural key the downstream system dedupes on. Re-execute; the
  far side collapses the duplicate.
- **`UNSAFE`** — an unguarded external effect. **Never blind-retried.** The step is marked
  `ERROR` with a `needs_review` reason and the run stops. If a tool wants automatic recovery it
  must earn it by becoming idempotent.

Making the effect class a *required field* on every tool is the design decision. It moves an
unanswerable distributed-systems question ("did it happen?") to the one place that can actually
answer it: the person writing the tool. (D-004.)

**Leases** solve the other half — two workers advancing the same run. Claiming is
`SELECT ... FOR UPDATE SKIP LOCKED` on a run whose lease has expired, which is atomic. The
check-then-act version of this is the same lost-update bug we hit in the QueueFair queue, in a
new costume.

---

## 7. Tool registry: Pydantic is the schema

A tool is a Pydantic input model, a callable, and metadata:

```
ToolSpec
  name          str                    # what the model calls
  description   str                    # the model's ONLY documentation — a prompt, not a comment
  input_model   type[BaseModel]        # validation AND schema, one definition
  effect_class  READ_ONLY | IDEMPOTENT_WRITE | UNSAFE      # §6
  execution     INLINE | DURABLE       # in-process, or dispatched to Celery
  timeout_s     int
```

`registry.schemas()` returns what goes into the `tools` parameter, with `input_schema` built by
`input_model.model_json_schema()`. One definition produces three things — the JSON Schema the
model plans against, the validator that parses what comes back, and the type the tool body reads.
They cannot drift, because there is only one of them.

Three consequences worth stating:

- **A schema mismatch becomes a `ValidationError` we can hand back** as a `tool_result` with
  `is_error: True`, so the model gets a chance to correct itself instead of the run dying.
- **`description` is prompt engineering, not documentation.** It is the only thing telling the
  model when to reach for this tool. It is reviewed like a prompt and versioned like one.
- **The tool list must be byte-stable across steps** or the prompt cache misses on every step. The
  registry emits schemas in sorted order for that reason alone — a one-line detail with a direct
  line to the monthly bill.

`INLINE` vs `DURABLE` is the F4 answer: a fast pure tool runs in the event loop; a slow one is
dispatched to Celery and executed there, while the loop keeps its lease and waits on the ledger
row the worker will write (**D-023**). The run does *not* change status and is not handed back —
an earlier draft of this section said it did, and the two halves of that sentence described
different architectures.

What the mode buys is precise: **the tool is not running in the process that can die.** An inline
tool killed mid-execution leaves the ambiguous t1–t2 window for `effect_class` to resolve; a
durable one finishes in its own worker and the resumed run finds the answer already recorded. A
tool still running when the step's budget expires pauses the run with `TOOL_PENDING`, which is a
pause and not a failure — nothing went wrong, the tool is just slower than one step may wait.

---

## 8. Multi-agent: delegation is just a nested run

A supervisor does not get a special execution path. `delegate` is an ordinary tool whose effect is
"start a child run and wait for its output":

```
   run A  (supervisor, budget 120k)
     │  step 3: tool_use delegate(agent="researcher", task="...", budget=40k)
     ├──────────────► run B  (parent_run_id=A, budget 40k, tool allowlist: retrieve, search)
     │                  └── its own steps, its own checkpoints, its own SSE stream
     ◄────────────── output returned as a tool_result block in A's step 4
```

Why nested runs rather than a second loop implementation:

- **Durability and resumption come free.** A child is a row in the same table with the same
  recovery rules. Nothing new to make crash-safe.
- **Context isolation is the actual point of multi-agent.** The child's fifteen retrieval results
  never enter the parent's context — only the child's *conclusion* does. That is a context-window
  argument, not an architectural aesthetic, and it is the honest reason multi-agent earns its
  keep.
- **Budgets compose.** The child's budget is *subtracted from* the parent's, so delegation cannot
  multiply cost. Without this rule, "let the agents figure it out" is an unbounded bill.
- **The allowlist is per-agent**, so a researcher physically cannot call a write tool. Capability
  restriction beats prompt instruction.

Cycle protection: a depth cap plus a `parent_run_id` walk, because "supervisor delegates to
supervisor" is one prompt away from an infinite tree.

---

## 9. Streaming: Redis Streams, not pub/sub

Every run has an event stream. Events: `run.started`, `step.started`, `token`, `tool.call`,
`tool.result`, `citation`, `step.completed`, `run.completed`, `run.failed`, `heartbeat`.

The design decision is **Redis Streams over pub/sub**, and it exists to make one specific user
experience work: the wifi drops for three seconds mid-answer, and the reconnecting client loses
nothing. Pub/sub has no memory — a subscriber that is not connected at publish time never learns
the message existed. A Stream is a log with monotonic IDs, so:

```
  SSE  id: 1737...-0     ──►  client stores it as Last-Event-ID
       (disconnect)
  GET /v1/runs/{id}/events   Last-Event-ID: 1737...-0
       ──► XRANGE from that id ──► replay the gap ──► then tail live
```

The SSE `id:` field *is* the Redis stream entry ID. No translation layer, no sequence numbers of
our own. `MAXLEN` and a TTL bound the memory, and those two settings are exactly "how long a
client may be disconnected and still catch up" — a product decision expressed as config.

**The scaling trap, stated up front:** one blocking `XREAD` per connection means Redis connections
scale with SSE clients. That is fine into the low hundreds and wrong after that. The known fix is
one reader task per process over sharded streams (`hash(run_id) % K`), fanning out to in-memory
`asyncio.Queue`s — the same shape as the QueueFair fan-out. We are **not** building that now:
per-run streams are simpler and correct, and the shard version is a change behind the `EventBus`
port. It gets built when a load test says so, not before. (D-005.)

---

## 10. Retrieval

```
 query
   │
   ├──────────────► lexical: tsvector @@ + ts_rank      ──► top 50 ranked ┐
   │                (GIN index, exact terms, IDs, rare words)             │
   │                                                                      ├─► RRF
   └──────────────► dense: embed(query) → pgvector <=>  ──► top 50 ranked ┘   fuse
                    (HNSW index, paraphrase & synonym)                         │
                                                                               ▼
                                                              cross-encoder rerank
                                                              (query, chunk) pairs
                                                                      │ top 8
                                                                      ▼
                                                            context + citation IDs
                                                                      │
                                                                      ▼
                                                                 answer with
                                                                 [chunk_id] refs
```

**Why hybrid.** The two retrievers fail in opposite directions. Dense search finds "how do I stop
the queue jumping" against a doc that says "admission fairness", and completely misses an exact
token like `ERR_2041` that appears nowhere in its training distribution. Lexical does the reverse.
Neither is a superset, so the fusion is not belt-and-braces — it is covering two distinct failure
modes.

**Why RRF and not a weighted score.** Cosine similarity and `ts_rank` are not on a shared scale,
and no normalisation makes them comparable in a principled way — you just get a magic weight to
hand-tune, which then overfits whatever you tuned it on. RRF throws the scores away and uses only
the ranks: `score(d) = Σ 1/(k + rank_i(d))`, k=60. One less tunable, and the one it removes is the
one most likely to be silently wrong.

**Why rerank on top of both.** Bi-encoders embed the query and document *separately* — they never
see them together, which is what makes the index possible in the first place. A cross-encoder
reads the pair jointly and is much more accurate, and much too slow to run over the corpus. So:
cheap retrievers propose 50, the expensive model judges 8. Classic recall-then-precision funnel.
It is also the p99 of the pipeline (§1 N4) and the first thing to cap when latency hurts.

**Citations** are carried structurally, not asked for in the prompt. Retrieved chunks enter the
context tagged with their `chunk_id`, the model is required to cite those IDs, and the API layer
resolves each ID back to `document.uri` plus `chunk.ordinal`. A cited ID that was never retrieved
is a **detectable** hallucination — a validation error, not a matter of opinion. That check is
worth more than any amount of "please only use the provided context".

**The open honesty problem:** `ts_rank` is not BM25. It has no document-length normalisation and
no IDF saturation, so calling it BM25 on a resume is a claim a retrieval-literate interviewer will
take apart. Either we implement real BM25 scoring or we change the wording. Tracked as **D-007**
and flagged in `resume-claims.md`; it does not get to quietly resolve itself.

---

## 11. Evaluation

Without this section the rest of the project is unfalsifiable.

**Two layers, deliberately.** Retrieval metrics are deterministic, cheap, and need no model:
recall@k, nDCG@10, MRR against a golden set of questions with known-relevant chunk IDs. They
carry the load. Answer quality then needs a judge — faithfulness (is every claim supported by a
retrieved chunk), citation validity (do the cited IDs exist and support the sentence), and
completeness against a reference answer.

**The judge is the least trustworthy instrument in the building**, so it is constrained: a fixed
rubric with explicit criteria, structured output so scores are parseable rather than prose,
randomised answer order to blunt position bias, and a cheaper model from a different tier than
the one being judged to reduce self-preference. Where the deterministic metrics can answer the
question, the judge does not get a vote.

**CI gate:** the eval runs on `main`, not every push (cost, CLAUDE.md §7). A regression beyond
threshold fails the build. Ablations are first-class — dense-only, lexical-only, hybrid,
hybrid+rerank all recorded on every run — because "hybrid RAG improved quality" is only a claim if
the numbers for the alternatives sit next to it. That table *is* the resume bullet's evidence.

---

## 12. Failure modes

| Failure | Detection | Behaviour | Data risk |
| --- | --- | --- | --- |
| Process killed mid-step | Lease expiry | Another worker resumes from last committed step | None (§6) |
| Killed between t1 and t3 | Ledger row `PENDING` | Effect class decides: retry, or stop for review | The one real ambiguity, made explicit |
| Anthropic 429 | `RateLimitError` | Bounded backoff, then fail the step, then fail the run | None; retries are visible in `attempt` |
| Model refusal | `stop_reason == "refusal"` | Terminal `FAILED` with the category, not a crash | None |
| Tool timeout | Per-step timeout | Step `TIMEOUT`, `is_error` result to the model, it may adapt | None |
| Runaway loop | Step/token cap | `BUDGET_EXCEEDED` | None — this is the cost guardrail firing |
| Redis down | Connection error | Runs continue; **streaming degrades**, history intact | None — Redis holds no truth |
| Postgres down | Connection error | Runs stop. Hard dependency, by design | None |
| Two workers, one run | Lease + `SKIP LOCKED` | Second worker declines the claim | None |
| Embedding model drift | Dim mismatch on insert | Ingestion fails loudly | Mixed-model vectors would silently poison recall |

The Redis and Postgres rows are the point of the split: one of them is allowed to take the site
down, and it is the one holding the truth.

---

## 13. Scale, and where it breaks

One box, Docker Compose. Deliberate — the interesting properties are correctness properties, and
they are all demonstrable on a laptop. Known ceilings, in the order they will actually bite:

1. **SSE connections** (§9) — per-connection Redis reads, fine to the low hundreds. Fix: sharded
   streams behind the existing port.
2. **Cross-encoder throughput** — CPU forward passes serialise across concurrent queries. Fix:
   batch, cache by `(query, chunk_id)`, or a GPU worker.
3. **Celery worker count** — each holds model weights in memory; RAM, not CPU, is the limit.
4. **Postgres write rate** — a few rows per step is nothing. This will not be the bottleneck, and
   claiming otherwise without a measurement would violate Rule 7.

The API is stateless and the lease makes multiple workers safe, so horizontal scale is a compose
`replicas` change, not a redesign. That is the claim worth being able to defend; actually running
five replicas proves nothing extra.

---

## 14. Security

- **The API key lives in `.env`, is read once in `settings.py`, and never enters a log, a run
  record, or a prompt.** Everything else here is secondary to that.
- **Tool arguments are model-generated input and are treated as hostile.** Pydantic validation is
  the gate, and it is not optional. A tool taking a path or a URL validates it explicitly — the
  directory-traversal class of bug arrives through a `tool_use` block exactly the way it arrives
  through a form field.
- **Retrieved chunks are untrusted text.** A document in the corpus can contain instructions
  aimed at the model. Retrieved content is fenced and labelled as data in the prompt, and
  `UNSAFE` tools are not on a RAG agent's allowlist — capability restriction, because prompt-level
  defences against injection are not reliable.
- **Every run carries a `tenant_id` and every retrieval query filters on it.** Cross-tenant
  retrieval leakage is the worst bug this system could have, so the filter belongs in the
  repository layer where it cannot be forgotten, not in each call site.
- Per-tenant rate limits on run creation; a run is expensive and unauthenticated run creation is
  a way to spend someone else's money.

---

## 15. Alternatives considered

Full entries with reasoning and revisit triggers are in `decisions.md`. Summary:

| Question | Chosen | Rejected | ID |
| --- | --- | --- | --- |
| Framework | FastAPI | async Django (used in QueueFair, deliberately not repeated) | D-001 |
| Agent loop | Manual loop | SDK `tool_runner` — owns the loop, no checkpoint hook | D-002 |
| Source of truth | Postgres | Redis (QueueFair's answer; wrong for an audit log) | D-003 |
| Exactly-once | Ledger + effect classes | Blind retry; distributed transactions | D-004 |
| Streaming | Redis Streams + SSE | Pub/sub (no replay); WebSockets (bidirectional, unneeded) | D-005 |
| Vector store | pgvector in the same DB | Pinecone, Qdrant, Weaviate | D-006 |
| Lexical scoring | **OPEN** — `ts_rank` vs real BM25 | — | D-007 |
| Embeddings | Local `bge-small` | Hosted embedding API | D-008 |
| Fusion | RRF | Weighted score normalisation | D-010 |
