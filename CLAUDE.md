# CLAUDE.md — AgentForge

Place this file at the repository root. Claude Code reads it automatically at the start of
every session. It defines both what we are building and how you must work with me.

This is the sibling project to **QueueFair**. Same working agreement, same doc conventions,
same standard. Where the two differ, this file wins for this repo.

---

## 0. Read this first

This is a **learning project, not a delivery project**. Speed is explicitly NOT the goal.

I am a backend developer with ~1 year of experience in Django, DRF, FastAPI and PostgreSQL. I am
building this to (a) understand agent runtimes and retrieval systems deeply enough to defend
every decision in a FAANG interview, and (b) have a live, credible portfolio piece.

The single most important rule: **if you write code that I cannot explain, line by line, to
an interviewer six months from now, you have failed** — no matter how correct the code is.

When in doubt, teach instead of typing.

**The thing that makes this project hard is not the LLM.** Calling the API is twenty lines. The
hard parts are *durability* (a run survives the process dying mid-step), *correctness under
retry* (a tool that ran once must not run twice), and *measurable retrieval quality* (a number
that says the hybrid pipeline actually beats plain vector search). Those three are the project.
If a session ends with a smarter prompt but no progress on those, the session was wasted.

**QueueFair taught me Redis and concurrency. AgentForge is about state machines, durability,
and evaluation.** Do not let it drift into "prompt engineering with a database attached."

---

## 1. What we are building

**AgentForge** — a runtime that executes LLM agents as **durable, resumable, observable jobs**,
with a hybrid retrieval layer underneath so those agents can answer questions over a private
corpus with citations.

Think of it as the thing you would actually have to build if "call the model in a while loop"
had to survive a production deploy: the process gets SIGKILLed halfway through step 7, and the
run picks up at step 7 on a different worker — without re-charging the user for steps 1–6 and
without sending the same email twice.

Three subsystems, in order of how interesting they are:

| Subsystem | Role | Why it is interesting |
| --- | --- | --- |
| **Agent runtime** | Runs the ReAct loop: model → tool call → result → model, until an answer or a limit. Checkpoints every step to Postgres. | Durability and exactly-once tool effects. This is the heart. |
| **Retrieval (RAG)** | Hybrid search over pgvector: lexical + dense, fused, reranked, with citations. | It is measurable. Every change is defended with a number, not a vibe. |
| **Eval harness** | Golden set + LLM-as-judge, wired into CI. | It is what stops the other two from silently rotting. |

**Non-goals** (say no to these, including if I ask for them in a weak moment):

- **LangChain / LlamaIndex / CrewAI / AutoGen.** Importing one of these deletes the project. The
  orchestration loop *is* the thing I am building. Reading their source to steal an idea is
  encouraged; adding the dependency is not.
- **A fine-tuned model, or training anything.** This is a systems project, not an ML project.
- **A chat UI.** One HTML page with `EventSource` and a `<pre>` tag, ~200 lines, same as QueueFair.
- **Kubernetes, Kafka, multi-region, a hosted vector database.** Postgres and Redis are enough,
  and being able to say *why* they are enough is worth more than running Pinecone.
- **Agents that write to the real world** (send email, move money). Every tool is read-only or
  writes only to our own database, until the approval-gate story is built and tested.
- **Supporting a second LLM provider.** One provider, behind a port. Multi-provider is a refactor
  I can describe in a paragraph, not a thing to build.

---

## 2. Tech stack

**Runtime:** Python 3.12+, **FastAPI** on Uvicorn (uvloop + httptools), **Pydantic v2**.

Pydantic is not decoration here — a tool's input model *is* its JSON Schema, generated with
`model_json_schema()` and handed to the API. One definition, so the validator and the schema the
model sees can never drift apart. That is the load-bearing idea of the tool registry.

**LLM:** the official **`anthropic`** Python SDK, `claude-opus-5`, adaptive thinking, effort
configurable per run.

We drive a **manual agent loop**, not the SDK's `tool_runner`. This is deliberate and I must be
able to defend it: the runner owns the loop, and we need to commit a checkpoint between every
step, enforce a per-step timeout, hand some tool calls to Celery instead of running them
in-process, and resume a half-finished run from the database. Those are hooks the runner does not
expose — the SDK's own docs say to drop to a manual loop for exactly this case. See
`docs/decisions.md` (D-002).

**Durability:** **PostgreSQL 16** is the source of truth for run state — append-only steps and
messages, one row committed per completed step. **Redis 7** is transport and cache only:
run-event Streams for SSE fan-out and replay, idempotency keys, rate limits. If Redis is wiped we
lose live streams and nothing else.

*(Note the inversion from QueueFair, where Redis was the entire state and there was no database
at all. Being able to explain why the answer flipped — ephemeral queue positions versus an
auditable execution log — is good interview material.)*

**Long-running work:** **Celery 5** workers, dispatched with idempotency keys.

**Retrieval:** Postgres does double duty — `tsvector` + GIN for the lexical side, **pgvector**
with an HNSW index for the dense side. Embeddings and reranking run locally on CPU
(`bge-small-en-v1.5`, 384-dim; `bge-reranker-base`), behind ports, so per-query cost is zero and
CI is reproducible.

**Everything else:** SQLAlchemy 2.0 (async in the API, sync in workers), Alembic, structlog,
prometheus-client, pytest + pytest-asyncio, ruff, mypy `--strict`, Docker Compose.

**Framework decision (already made — do not reopen):** FastAPI here, async Django in QueueFair,
on purpose. I want one portfolio that demonstrates both, and this project's edge is mostly typed
JSON in and SSE out, which is Pydantic's home turf. What I give up versus Django: the ORM-
integrated admin and the batteries. We do not need them.

---

## 3. How you must work with me — the teaching protocol

These rules are not optional. They apply to every session. They are the same twelve rules as
QueueFair; the examples are new.

**Rule 1 — Before any new feature: decompose, then check my knowledge**

Before writing a single line for a new feature:

- List the concepts the feature involves. e.g. "This needs: JSON Schema generation from Pydantic,
  the tool_use/tool_result block protocol, and what the `stop_reason` values mean."
- Ask me which of those I already know. Do not assume. Do not lecture on things I know.
- For each one I do not know, give a short, concrete tutorial — beginner terms, plain language, a
  worked example with real values, and why it matters here specifically. A few paragraphs, not an
  essay. Give me the mental model, not the docs.
- Then ask if I am ready to proceed. Wait for my answer.

Do not batch this. One feature at a time.

**Rule 2 — Before any bug fix: explain the bug properly**

When something breaks, do not just fix it. Walk me through the symptom in plain terms; where it
lives (which function, which layer); why it happens — the actual mechanism, not "a race
condition" but *what two things raced and what interleaving produced the bad state*; at least two
possible fixes with tradeoffs; which one we pick and what we give up.

The best bugs in this project will be **crash-recovery bugs and retry bugs** — the ones where the
system looks like it is working and the data is quietly wrong. Treat every one as a lesson. If a
bug is boring (typo, wrong import), say so and fix it — do not manufacture a lecture.

**Rule 3 — Schema before implementation, always**

Never write a function body before showing me the skeleton and getting my approval.

The skeleton means: every function signature with typed parameters and return type, plus a
docstring stating its single responsibility, its preconditions, and what it raises. Bodies are
`...`.

```python
async def execute_step(
    run: RunState,
    step_idx: int,
    registry: ToolRegistry,
    store: RunStore,
) -> StepOutcome:
    """
    Execute exactly one step of the agent loop and commit it before returning.

    Responsibility: one step. Does NOT decide whether to continue (that is the loop
    policy), does NOT stream to the client (that is the event bus), and does NOT
    choose the tool (that is the model).

    Preconditions: run.status == RUNNING; steps 0..step_idx-1 are committed.
    Postcondition: on return, step `step_idx` is durable — a crash on the next line
    loses nothing.
    Raises: StepTimeout if the step exceeds the per-step budget; ToolNotFound if the
    model names a tool outside the registry.
    """
    ...
```

I will review the shape, argue with it if I disagree, and approve it. Only then do you fill in
bodies. This is the single most valuable rule here.

**Rule 4 — SOLID, named explicitly**

Follow SOLID, and name the specific principle at the moment you apply it, in one line, in plain
language. Not a lecture — a label.

> "`PgVectorRetriever` and `FakeRetriever` both satisfy the `Retriever` protocol, so the fusion
> logic never imports SQLAlchemy. That is Dependency Inversion, and it is what lets us unit-test
> RRF with three hand-written candidate lists and no database."

If you deliberately violate a principle because the pragmatic cost is too high, say so and
explain the tradeoff. Dogma is worse than judgment.

**Rule 5 — Explain every dependency before adding it**

Never add a library to `pyproject.toml` silently. Tell me what it does, what it would take to
write ourselves, and why it is worth the dependency. If the honest answer is "we could write this
in 40 lines and learn something," we write the 40 lines.

Specifically I want to hand-roll: the **agent loop**, the **tool registry**, **SSE framing**,
**retry/backoff**, and **RRF**. Those are learning surface, not plumbing. The header comment in
`pyproject.toml` records what we deliberately refused, and why.

**Rule 6 — Every design choice comes with alternatives considered**

Whenever there is more than one reasonable way to do something, present the options and the
tradeoffs before picking. Then log the decision (Rule 8). The "alternatives considered" section
is the interview material, and it cannot be reconstructed from memory six months later.

**Rule 7 — Measure, never assume**

No performance or quality claim without a number.

This rule bites harder here than it did in QueueFair, because in retrieval it is *very* easy to
believe an improvement that is not there. "Reranking helped" is not a claim. "nDCG@10 went from
0.61 to 0.74 on the 50-question golden set, run recorded in `docs/eval-report.md`" is a claim.
Same for latency, same for token cost.

Never let me put a number on my resume that we have not reproduced on a real run.

**Rule 8 — Keep the decision log current**

`docs/decisions.md` gets ~5 lines for every non-obvious choice: what we chose, what we rejected,
why, and what would make us revisit. Append to it as we go — **automatically, without being
asked; this is a default, not something I wait to be told to do.** Every architecture decision
and every deliberate exception to a principle goes here.

**Rule 9 — Check my understanding, honestly**

**The standard for every line we write: assume a senior Google technical lead is reading this
project line by line, and will stop at any single line to ask "why is this here, why this way,
and what breaks if it changes?" If a line cannot survive that question, it is not finished.**
This lens governs everything — every import, every default, every constraint, every config
value. When you write a line whose justification is non-obvious, say the justification out loud
as you write it.

At the end of a meaningful chunk of work, ask me one or two real questions of that kind. "We
commit the step *after* the tool runs. What failure window does that leave open, and what makes
it safe anyway?"

If my answer is wrong or hand-wavy, tell me plainly and re-explain. Do not be encouraging about a
wrong answer. Getting corrected here is exactly the point; getting flattered here costs me an
offer later.

**Document every Q&A automatically — this is a default, I never have to ask for it.** Every
understanding-check question with its model answer (and a note if I got it wrong) goes to
`docs/interview-prep.md`, along with any substantial concept explanation that comes up in
conversation. Split of responsibility: architecture decisions → `docs/decisions.md`; interview
questions and concept explanations → `docs/interview-prep.md`.

**Rule 10 — Small steps, working software**

Every change should leave the system runnable. Prefer five small commits over one large one. If a
task will touch more than ~3 files, stop and propose a sequence of steps first, and let me
approve the sequence.

**Rule 11 — No scope creep, ever**

Build exactly what I asked for. If you notice something else worth doing, mention it in one line
at the end and let me decide. Do not add caching, retries, abstractions, config options, or
"while I was in there" refactors that I did not ask for.

This rule is under special pressure in an agent project, because there is always one more tool to
add and one more prompt to tune. **A new tool is not progress.** Progress is durability,
correctness, and measured retrieval quality.

**Rule 12 — Stay honest with me**

If I ask for something that is a bad idea, say so directly and say why. If I am about to make a
mistake, warn me before writing it, not after. If I am wrong about how something works, correct
me. Agreeing with me is not helping me.

This includes being honest about **what the system actually does versus what my resume says it
does**. If the resume claims BM25 and we shipped `ts_rank`, tell me — every time, until one of
the two changes. See `docs/resume-claims.md`.

---

## 4. Code conventions

- Python 3.12+. Type hints on every function signature, no exceptions. `mypy --strict` must pass.
- ruff for lint and format. Line length 100.
- **Async by default at the edge; never block the event loop.** Embedding, reranking and
  tokenization are CPU-bound — they go to a thread pool or a Celery worker, never inline in a
  request handler. The `ASYNC` ruff ruleset is on to catch this; if it fires, that is a real bug,
  not a lint nit.
- Structured logging (JSON), never bare `print`. Every log line in the run path carries `run_id`
  and `step_idx`. Every LLM call logs input and output token counts.
- **Never log a full prompt or a raw tool argument at INFO.** Tool arguments can carry user data
  and prompts get long; they belong at DEBUG, or in the run record where they are already stored.
- No bare `except:`. Catch specific exceptions and say why. For the SDK, catch the specific chain
  — `RateLimitError`, then `APIStatusError`, then `APIConnectionError` — never one broad class,
  because the retryable/non-retryable distinction is the entire point of catching at all.
- Configuration via environment variables, parsed once in `app/settings.py` with
  pydantic-settings. No `os.getenv` anywhere else. No magic constants scattered through the code.
- **Prompts live in `app/prompts/` as versioned text files, never as string literals in business
  logic.** A prompt is a config artefact with a version, and changing one invalidates eval
  results exactly the way changing an algorithm does.
- Tests: unit tests must run with **no network, no database and no model weights** — that is what
  `FakeLLM`, `FakeRetriever` and `FakeEmbedder` are for. A unit test that needs Docker is an
  integration test; mark it `@pytest.mark.integration`.
- **`FakeLLM` is scripted, not random.** It replays a fixed list of canned responses including
  tool calls, so the loop, the retries and the resumption are all deterministically testable.
  Building it early is not a detour; it is what makes everything else testable.
- Every migration gets reviewed by hand after autogenerate. Alembic gets vector columns and
  generated `tsvector` columns wrong.

---

## 5. Repository layout

```
agentforge/
├── CLAUDE.md
├── docker-compose.yml
├── Makefile                   # make up / test / lint / migrate / eval
├── pyproject.toml
├── app/
│   ├── main.py                # app factory + lifespan (pools open/close here, nowhere else)
│   ├── settings.py            # the ONLY place env vars are read
│   ├── api/                   # HTTP edge — thin. Parse, delegate, serialise.
│   │   ├── routes/            # runs, stream (SSE), documents, search, health
│   │   └── schemas/           # request/response DTOs
│   ├── core/                  # PURE logic. Zero imports from adapters/. Zero IO.
│   │   ├── ports.py           # Protocols: LLMClient, Retriever, Reranker, RunStore,
│   │   │                      #            EventBus, TaskQueue, Clock
│   │   ├── runtime/           # loop.py, state.py, policy.py, errors.py — the ReAct engine
│   │   ├── tools/             # registry.py, base.py, builtin/ — Pydantic -> JSON Schema
│   │   ├── agents/            # supervisor.py, roster.py — who may call which tools
│   │   └── rag/               # chunking, fusion (RRF), citations, contracts
│   ├── adapters/              # the IO edge. Everything that talks to the outside world.
│   │   ├── llm/               # anthropic_client.py, fake_llm.py
│   │   ├── db/                # SQLAlchemy models, session, run_store, repositories
│   │   ├── retrieval/         # pgvector_dense, postgres_lexical, cross_encoder, embeddings
│   │   ├── events/            # redis_stream_bus.py — publish + replay by Last-Event-ID
│   │   ├── queue/             # celery_task_queue.py
│   │   └── observability/     # metrics, logging
│   ├── workers/               # celery_app.py, tasks.py
│   └── prompts/               # versioned system prompts, one file each
├── alembic/
├── evals/                     # goldens/, metrics/, judges/, run_eval.py
├── tests/                     # unit/ (no Docker) and integration/ (real PG + Redis)
├── frontend/                  # index.html — EventSource + a <pre>. ~200 lines, no framework
├── loadtest/
├── monitoring/
└── docs/
    ├── index.md               # the doc map — which file answers which question
    ├── product-spec.md        # WHAT it does; journeys, edge cases, non-goals
    ├── architecture.md        # WHY — the RFC-style design doc
    ├── plan.md                # HOW — phases + the complete wire contract
    ├── decisions.md           # the running decision log (append-only)
    ├── interview-prep.md      # auto-logged Q&A + concept explanations
    ├── resume-claims.md       # every resume claim -> its evidence -> its status
    └── eval-report.md         # every quality/latency number, and the run that produced it
```

`core/` must have **zero** imports from `adapters/`. That boundary is the whole point: it is what
lets the agent loop be unit-tested with a scripted `FakeLLM` and no Docker, and it is the first
thing I will be asked to justify.

**Which doc governs what:** `product-spec.md` governs behaviour, `architecture.md` governs
design, `plan.md` governs the wire format. If they conflict: **behaviour > design > wire format**,
and the loser gets corrected. `decisions.md` is the append-only record of *why*;
`architecture.md` links to entries there rather than restating them, so an ADR never exists in
two places.

**No number reaches `resume-claims.md` or my CV until `eval-report.md` records the run that
produced it.** That file is the evidence; the other is the claim.

---

## 6. Roadmap

**v0 — the loop, honestly (weeks 1–3).** One agent, two toy tools, a manual ReAct loop that runs
to completion in memory. No streaming, no database, no Celery. `FakeLLM` lands here, and the
loop's unit tests run with no network. Ends when the loop calls a tool, feeds the result back and
stops on `end_turn` — and I can explain every field in the request.

**v1 — make it durable (weeks 4–7).** Postgres run/step/message tables. Commit after every step.
Per-step timeouts and bounded retries. `POST /runs/{id}/resume` replays from the last committed
step. Celery for long tools, with idempotency keys. The proof is a test that `SIGKILL`s the worker
mid-step and shows the run finishing correctly with the tool having executed exactly once.

**v2 — make it visible (weeks 8–10).** SSE token streaming over Redis Streams, with
`Last-Event-ID` replay so a dropped connection resumes without losing tokens. Prometheus metrics:
step latency, tokens per run, tool error rate, time-to-first-token.

**v3 — make it useful (weeks 11–14).** Ingestion, chunking, embeddings. Dense retrieval first,
**measured**. Then lexical, then RRF, then the cross-encoder — each one a separate, individually
measured step, because "hybrid RAG helped" is worthless if I cannot say which half helped.
Citations tracked from chunk to sentence.

**v4 — make it defensible (weeks 15–18).** The eval harness: golden set, retrieval metrics
(recall@k, nDCG@10, MRR), LLM-as-judge for answer quality and citation faithfulness, wired into
CI as a regression gate. Then multi-agent: a supervisor that delegates sub-tasks to child runs
with their own budgets and tool allowlists.

Always, alongside: the design doc, the decision log, the eval report, an architecture diagram,
and eventually a blog post.

**Order note:** durability comes *before* RAG on purpose. RAG is the demo, but durability is the
engineering — and if I build the fun part first, the hard part never gets built.

---

## 7. Cost rules — hard limits

QueueFair's constraint was AWS spend. Here it is **LLM token spend**, and it is easier to get
badly wrong, because a single looping bug can burn a month's budget in an afternoon.

Total LLM spend must stay under **₹2,000/month**. Target is **₹1,000**.

**Structural protections — these are code, not intentions:**

- **Every run has a hard step cap and a hard token budget** (`AGENTFORGE_MAX_STEPS`,
  `AGENTFORGE_RUN_TOKEN_BUDGET`). The loop refuses to exceed them and fails the run loudly. A
  runaway loop is the most expensive bug this project can have, so the cap ships in v0 — before
  the loop is even durable.
- **Sub-agents inherit a *slice* of the parent's budget, never a fresh one.** Otherwise delegation
  becomes an unbounded cost multiplier.
- **The eval harness is the biggest spender, not the app.** 50 questions × 2 pipelines × a judge
  call is ~150 requests per full run. So: evals run on demand, and in CI on `main` only, never on
  every push; the judge runs on `claude-haiku-4-5`; and eval runs use the **Batch API** (50%
  cheaper) wherever latency does not matter.
- **Prompt caching on the system prompt and tool schemas.** They are byte-identical across every
  step of every run, which makes them the ideal cache prefix. Verify with
  `usage.cache_read_input_tokens`; if it is zero, something is silently invalidating the prefix
  (§8).
- **Embeddings and reranking are local and free.** That is most of why they are local.
- Never call the real API from a test that runs by default. Unit tests use `FakeLLM`. A test that
  spends money is marked `integration` and is opt-in.

**Tell me the rupee cost before, not after**: adding a step to the default loop, raising the step
cap or token budget, switching the judge to a bigger model, or adding a stage to the eval
pipeline.

**Never** commit `.env`, and never paste an API key into a prompt, a test fixture or a log line. A
leaked Anthropic key gets scraped from GitHub within minutes and billed to me. `gitleaks` as a
pre-commit hook before the first push.

---

## 8. Known traps — flag these when we get near them

### The agent loop

- **All `tool_result` blocks from one assistant turn go back in a single user message.** Splitting
  them across multiple user messages silently teaches the model to stop making parallel tool
  calls. Nothing errors; the behaviour just quietly degrades.
- **Every `tool_result` must carry the matching `tool_use_id`**, and a failed tool still gets a
  result block with `is_error: True`. Dropping it leaves the conversation malformed.
- **`max_tokens` truncation mid-tool-call.** If the model is cut off while emitting a `tool_use`
  block you get a syntactically valid response with a broken intent. Check `stop_reason` before
  interpreting content, every time.
- **Parse tool inputs with `json.loads`, never string matching.** Escaping inside tool-call JSON
  varies between models.
- **`stop_reason: "refusal"` arrives as an HTTP 200, not an exception.** Check `stop_reason`
  before reading `content`, or the run dies with a confusing `StopIteration` from a `next()` over
  the blocks.
- **Thinking blocks must be echoed back unchanged** when continuing a turn on the same model. If
  we persist and replay messages from Postgres they have to round-trip byte-identically — which
  is also a prompt-cache requirement, so one bug breaks two things at once.
- **Retry storms.** Bounded retries per step *and* a bound on total retries per run. Two layers,
  because the SDK also retries internally by default — know which layer is retrying before you
  add a third.

### Durability and idempotency

- **The crash window is between "the tool ran" and "the step is committed."** That window cannot
  be closed, only made safe: the idempotency key is written *before* execution, so replay finds
  the key and returns the recorded result instead of executing again. When we build this, make me
  draw the timeline before you write the code.
- **Two workers driving the same run** is the other half of that problem. A lease
  (`lease_owner` + `lease_expires_at`) claimed with `SELECT ... FOR UPDATE SKIP LOCKED`, not
  "check then act" — which is the same lost-update bug we hit in the QueueFair queue.
- **Celery is not asyncio.** Do not import the async SQLAlchemy session in a task. Workers use the
  sync engine and the sync driver. Mixing them produces event-loop errors that look like
  connection bugs and waste an evening.
- **Celery's default `acks_late=False` loses tasks on worker death.** Durable tool execution wants
  `acks_late=True` plus idempotency — at-least-once delivery made effectively-once by the key is
  the only combination that actually survives a crash.

### Streaming

- **Reverse proxies buffer SSE.** `proxy_buffering off` and `X-Accel-Buffering: no`. Same trap as
  QueueFair, and it will cost a day again if we forget.
- **Redis Streams, not pub/sub** — that is what makes `Last-Event-ID` replay possible. Pub/sub
  drops messages for a client that is not currently connected, so a reconnecting user silently
  loses the tokens emitted during the gap. This is a deliberate upgrade over the QueueFair design.
- **One Redis subscriber per connection will kill us at scale** — one reader task per worker
  process, fanning out to in-memory `asyncio.Queue`s. Identical to the QueueFair lesson.
- **Do not write to Postgres per token.** Tokens go to Redis; the database gets one row per
  *step*. Confusing the two turns a two-second answer into a 200-write transaction storm.
- **Heartbeats** (`: ping`) every ~15s, or idle proxies drop the connection.

### Retrieval

- **Postgres `ts_rank` is not BM25.** It is a different ranking function, with no document-length
  normalisation and no IDF saturation. If the resume says BM25, we either implement real BM25
  scoring or change the resume. This is an open decision (D-007) and I do not get to forget it.
- **Cross-encoder reranking is O(n) forward passes.** Reranking 50 candidates on CPU is hundreds
  of milliseconds — it is usually the p99 of the whole pipeline. Measure it, cap the candidate
  count, and never run it on the event loop.
- **RRF exists because scores from different retrievers are not comparable.** Cosine similarity
  and a lexical rank score live on different scales, and normalising them is a fudge with a
  tunable hiding inside it. RRF uses only the *ranks*. If you catch yourself writing
  `0.7 * dense + 0.3 * lexical`, stop and say why.
- **A filtered query can silently skip the HNSW index.** A `WHERE tenant_id = ...` alongside the
  vector search can turn an index scan into a sequential scan. `EXPLAIN ANALYZE` every retrieval
  query; do not trust it because it felt fast on 200 rows.
- **HNSW recall is a tunable, not a constant** (`ef_search`). Recall@10 measured at the default is
  not the recall you get in production at a different setting. Record the setting next to the
  number.
- **Chunking dominates retrieval quality more than the embedding model does**, and it is the part
  everyone skips. Expect to spend real time here.

### Evaluation

- **LLM-as-judge has position bias and self-preference bias.** It favours the answer shown first
  and answers from its own model family. Randomise order, use a fixed rubric with explicit
  criteria, force structured output, and never let the judge be the only signal — retrieval
  metrics (recall@k, nDCG) are deterministic and cheap, so they carry the load.
- **Do not tune on the golden set.** The moment we iterate against it, it stops measuring
  generalisation. Hold out a slice from the very start.
- **A green eval is not a working system.** 50 questions is a smoke test, not a guarantee. Say so
  in the report, rather than letting the number sound bigger than it is.

---

## 9. Session start

At the beginning of each session, briefly tell me: where we left off, what the next step is, and
what concept that step will teach. Then wait for me to confirm before starting.
