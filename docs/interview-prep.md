# Interview Prep — AgentForge

Auto-logged per CLAUDE.md Rule 9. Every understanding-check question with its model answer, plus
any substantial concept explanation that came up in conversation. Architecture *decisions* live in
`decisions.md`; this file is the study material.

Format: **Q**, then the answer I should be able to give, then a note if I got it wrong when asked.

---

## Seeded during project planning · 2026-08-20

These came out of the design session before any code existed. They are the questions the design
itself raises, so they are the ones most likely to be asked.

---

### Q1 — Why not use the SDK's `tool_runner`? Isn't hand-writing the agent loop reinventing the wheel?

Because the runner owns the loop, and the loop is where every one of my requirements lives. I need
to commit a durable checkpoint between steps, enforce a per-step timeout separate from the run
timeout, route some tool calls to Celery instead of executing them in-process, and resume a
half-finished run from Postgres on a different machine. The runner exposes none of those hooks —
it is designed for the case where the loop is uninteresting plumbing.

The honest framing: the runner is the right choice for most applications, and I would use it if I
were shipping a product. Here the loop *is* the product. The SDK's own documentation says to drop
to a manual loop when you need control it does not expose, which is exactly this case. (D-002.)

---

### Q2 — You checkpoint after the tool runs. What breaks if the process dies in between?

Nothing breaks, but there is a real ambiguity and pretending otherwise would be the wrong answer.

The sequence is: write the idempotency-ledger row as `PENDING` and commit; execute the tool; then
commit the result and the step row in one transaction. If we die between those two commits, the
ledger says `PENDING` and we genuinely **cannot tell** whether the side effect happened — the
effect is not in our database, so no transaction covers both.

That is why exactly-once is not achievable and I do not claim it. What is achievable is
at-least-once delivery with effectively-once outcomes, and only for tools that can be deduped. So
every tool declares an effect class: `READ_ONLY` is re-executed freely, `IDEMPOTENT_WRITE` is
re-executed because the downstream deduplicates on a natural key, and `UNSAFE` is never
blind-retried — the run stops and a human resolves it.

The design decision is making that class a *required field* on every tool. It moves an
unanswerable runtime question to the one place that can answer it: the person writing the tool,
at definition time. (D-004, `architecture.md` §6.)

---

### Q3 — Why Redis Streams instead of pub/sub? You used pub/sub in QueueFair.

Different requirement, so a different answer — and noticing that is the point.

In QueueFair, a waiter who reconnects only needs their *current* queue position. Messages sent
while they were away are worthless, so pub/sub's fire-and-forget is a perfect fit and cheaper.

Here, a client that drops for three seconds mid-answer must not lose the tokens generated in that
gap. Pub/sub has no memory: a subscriber not connected at publish time never learns the message
existed. A Stream is a log with monotonic IDs, which is exactly the replay primitive SSE's
`Last-Event-ID` header was designed around. I use the Redis entry ID directly as the SSE `id:`
field, so reconnect becomes `XRANGE` from that ID and then tail — no sequence numbers of my own
to keep in sync.

The cost I should volunteer before being asked: one blocking `XREAD` per SSE connection, so Redis
connections scale with clients. Fine into the low hundreds, wrong after that; the fix is sharded
streams with one reader task per process fanning out to in-memory queues — the same shape as the
QueueFair fan-out. It sits behind the `EventBus` port and gets built when a load test demands it.
(D-005.)

---

### Q4 — Why RRF instead of just weighting the two scores?

Because cosine similarity and a lexical rank score are not on a comparable scale, and there is no
principled way to make them comparable. Min-max normalisation is corpus-dependent; z-scores assume
a distribution neither has. Any weighted blend introduces a coefficient that I would then tune,
and I would tune it on whatever test set I have, which is overfitting with extra steps.

RRF throws the scores away and uses only ranks: `score(d) = Σ 1/(k + rank_i(d))`, with k=60 from
the original paper — k damps the influence of the very top ranks so one retriever cannot dominate
the fusion.

What I give up: real score *margins*. RRF cannot distinguish "rank 1 by a mile" from "rank 1 by a
hair". That loss is bounded, because the cross-encoder immediately downstream produces genuine
scores over the fused candidates. (D-010.)

---

### Q5 — Why is hybrid better than dense alone? Isn't semantic search strictly more powerful?

No, and this is the question that separates people who have run retrieval evals from people who
have read about them. The two fail in *opposite* directions.

Dense retrieval handles paraphrase and synonymy: "how do I stop people jumping the queue" finds a
document that says "admission fairness" with no shared vocabulary. But it is bad at exact rare
tokens — an error code like `ERR_2041`, an identifier, a surname — because those are barely
represented in the embedding space and land nowhere useful.

Lexical search is the mirror image: perfect on the exact token, useless on the paraphrase.

Neither is a superset of the other, so the fusion is not redundancy for safety — it covers two
distinct failure modes. And I should be able to say by how much, per corpus, from the ablation
table rather than from theory.

---

### Q6 — Your resume says BM25. Does Postgres full-text search implement BM25?

**No — and this is a trap I set for myself.** `ts_rank` and `ts_rank_cd` are Postgres's own ranking
functions. They have no document-length normalisation and no IDF saturation, and they handle term
frequency differently. Calling them BM25 is wrong.

Three honest resolutions: ship `ts_rank` and write "lexical (Postgres FTS) + dense" on the resume;
implement real BM25 scoring in SQL; or add the `pg_search`/ParadeDB extension, which does
implement BM25 in Postgres. It is tracked as an open decision (D-007) and as a blocking item in
`resume-claims.md`, and it must be resolved before the bullet is finalised.

The meta-lesson worth stating out loud in an interview: I found this by writing the design doc
before the code, which is most of why the doc exists.

---

### Q7 — Why is Postgres the source of truth here when Redis was in QueueFair?

Because the state changed shape, and reusing the previous answer would be cargo-culting.

QueueFair's state was ephemeral and high-churn — a queue position is worthless sixty seconds
later, nobody audits it, and it needs one atomic operation per admission. That is Redis's shape.

A run is an execution record: it must survive a crash, be queried after the fact, support
`UNIQUE(run_id, idx)` and foreign keys, and commit a step row and a ledger row in one transaction.
That is a database.

So Postgres holds truth and Redis holds transport and cache — flush Redis and I lose live streams
and nothing else. Being able to explain why the same engineer chose opposite answers on two
projects is worth more than being consistent. (D-003.)

---

### Q8 — What stops a runaway agent from costing you a fortune?

Three structural things, not intentions.

Every run carries a hard step cap and a hard token budget, and exceeding either is a first-class
terminal state (`BUDGET_EXCEEDED`), not an error — running out of budget is an *expected* outcome
of an open-ended loop, and the operator response differs from a crash. Second, sub-agents inherit a
*slice* of the parent's remaining budget rather than a fresh one, so delegation cannot multiply
cost. Third, the caps shipped in v0, before the loop ever met a real API key, because a runaway
loop is the single most expensive bug this project can have.

The non-obvious part: the eval harness, not the app, is the biggest spender — 50 questions × 4
pipelines × a judge call. So evals run on demand and on `main` only, the judge runs on a cheaper
model, and eval runs use the Batch API at half price where latency does not matter.

---

### Q9 — Why does `chunks.tsv` need to be a generated column?

Because otherwise it is an invariant maintained by application code, and application code forgets.
If any path updates `chunks.text` without recomputing `tsv`, lexical search silently returns stale
results — no exception, no failing test, just quietly worse retrieval that would surface weeks
later as an unexplained eval regression.

A `GENERATED ALWAYS AS (...) STORED` column makes the drift impossible rather than merely
unlikely. Same reasoning as the `UNIQUE(run_id, idx)` constraint on steps: push the invariant into
the database where it cannot be bypassed.

---

### Q10 — Your loop streams tokens. Why don't you persist them?

Tokens go to Redis; Postgres gets one row per *step*. A two-second answer is a few hundred tokens,
so persisting per token would turn one logical step into a couple of hundred write transactions —
the checkpoint overhead budget (15ms p95) would be blown by two orders of magnitude, and the
durability design would collapse under its own bookkeeping.

The insight is that tokens and steps have different durability requirements. A lost token is a
cosmetic problem, recoverable by replaying from the Redis stream or, worst case, by the final
answer arriving whole in `run.completed`. A lost step is a correctness problem. Matching the
storage to the requirement instead of storing everything the same way is the whole trick.

---

## v0.1 — settings, app factory, `/healthz` · 2026-09-19

---

### Q11 — `max_steps` carries `le=50` in the settings model. The loop already enforces the step cap, so what does a bound on the *config* protect against?

Against the configuration itself being wrong. The loop enforces the limit against whatever value
it was handed — its job is to obey the config, not to audit it. So a typo in `.env` that writes
`120` instead of `12` survives any amount of loop enforcement, and the first symptom is the bill.

`le=50` turns that silent 10x cost increase into a startup failure, which is the right failure: it
is loud, it happens before a single token is spent, and it is attributable to the deploy that
caused it. The general principle is that a guardrail which can itself be misconfigured is not a
guardrail — the enforcement and the bound on the enforcement are two separate protections, and the
cheap one belongs in the type. (CLAUDE.md 7.)

---

### Q12 — `/healthz` deliberately performs no IO. Why is a health check that pings Postgres actively harmful rather than merely wasteful?

Because something *acts* on the answer. If liveness fails while Postgres has a bad minute, the
orchestrator restarts the API containers — all of them, simultaneously, because they all share
that dependency. The restarts discard warm connection pools and every in-flight request, and the
reconnect storm makes the database outage worse. One dependency blip becomes a full application
outage, caused entirely by the probe.

The distinction is what the remedy is. Liveness asks "is this process wedged?", and the only
remedy for yes is a restart — so it must fail only for conditions a restart actually fixes.
Readiness asks "should traffic be routed here?", and its remedy is to stop routing traffic, which
is harmless and reversible. Dependency checks belong in `/readyz` for exactly that reason.
(plan.md 2.6.)

---

## v0.2 — ports and the message vocabulary · 2026-09-19

---

### Q13 — The Anthropic SDK ships exact types for messages and content blocks, and its docs say not to redefine them. You did anyway. Defend that.

The advice is right for an application that calls the model, and wrong for this one, because of
where the types would land. `core/` holds the agent loop, and `core/` importing `anthropic` means
the unit suite depends on the provider SDK, `FakeLLM` has to construct real SDK objects to stand
in for responses, and the boundary that makes the loop testable with no network stops being a
boundary. The loop is the product here; it does not get to be coupled to a vendor.

What I would *not* defend is the naive version of owning the types — modelling every block with
fields. Thinking blocks have to be echoed back unchanged, so parsing one into my own fields and
re-serialising it is the exact mechanism by which the round trip breaks. So I only give fields to
the three block kinds the loop interprets: text, tool_use, tool_result. Everything else is opaque
and carried through untouched.

The cost is a translation layer in the adapter, and I keep it honest with an opt-in live test
that checks the translation against a real response rather than my memory of the wire format.
(D-014.)

---

### Q14 — Thinking blocks stay opaque while text and tool_use get real fields. What bug does that prevent, and why would it be hard to find?

It prevents mangling a block on the way back out, and the reason it is hard to find is that
nothing raises.

Continuing a turn on the same model requires the thinking blocks to come back unchanged. If I
parse one into a typed model and re-serialise it, anything I get slightly wrong — a dropped
`signature`, a re-ordered key, a field I did not know about — produces a request that is still
syntactically valid. The model does not reject it. It just behaves worse.

And the same bug has a second symptom in a different system: the conversation prefix is the
prompt cache key, so a block that does not reproduce byte-identically also stops the cache
matching, and I find out on the bill rather than in a traceback. One bug, two silent failures, no
exception anywhere.

A block with no parsing step has nothing to get wrong. The check that it is working is
`usage.cache_read_input_tokens` — zero across repeated runs means something is invalidating the
prefix. (CLAUDE.md 8, D-014.)

---

## v0.3 — the typed tool registry · 2026-09-19

---

### Q15 — Your registry sorts tool schemas by name. Why would the *order* of a list of tool definitions ever matter?

Because it is the front of the prompt-cache prefix. The request renders `tools`, then `system`,
then `messages`, and caching is a prefix match — a changed byte anywhere invalidates everything
after it. The tool array is the first thing in that prefix, so its order decides whether any of
the rest of the request can be a cache hit.

With insertion order, the array depends on import order, which moves silently during a refactor
and can differ between two processes that built their registries along different code paths. The
result is workers that never hit each other's cache, on every step of every run. Nothing errors.
The only symptom is `usage.cache_read_input_tokens` sitting at zero and the bill going up.

Sorting removes the variable. Adding a tool still invalidates the prefix, but that is a real
change to what the model is told, not an accident. (D-016.)

---

### Q16 — "The Pydantic model is the JSON Schema" — isn't that just saving you from writing a dict by hand?

Writing the dict is the cheap part. What it buys is that there is no *second place* to update.

A hand-written schema plus a hand-written validator are two declarations of the same contract,
and they drift the first time someone edits one of them. The drift is silent and it fails in the
worst direction: the model plans against a schema saying one thing, the handler enforces a rule
saying another, and the symptom is a tool call that cost money to produce and then failed
validation for a reason the model had no way to anticipate.

Concretely: `precision: int = Field(ge=0, le=10)` is one declaration. It becomes `minimum: 0,
maximum: 10` in the schema the model plans against, *and* the rule that rejects `precision=99`
when the model replies. One source, two consumers, no way for them to disagree. That is the
claim, and the test that proves it asserts both halves from the same spec.

---

## v0.4 — the first two tools · 2026-09-19

---

### Q17 — Your calculator parses an AST instead of calling eval(). Is that not over-engineering a toy?

No, because of where the string comes from. The expression is written by the model, and the
model's context contains retrieved documents and user-supplied text. So anything that can
influence either of those can influence what gets evaluated — `__import__("os").system(...)` is
one prompt injection away, and it is a valid Python expression that `eval` runs without
complaint. "The model would not do that" is not a security property; the model is not the
attacker, the person who wrote the document it retrieved is.

So the tool parses the expression with `ast.parse` and walks the tree, permitting an explicit
list of node types — numeric constants, unary plus and minus, seven binary operators — and
refusing everything else by default. The default branch is the design: a blocklist of dangerous
node types would need updating every time the language grows a feature, and would be wrong in the
meantime.

Two things fall out that are not about injection at all. `9 ** 999` is not an error in Python, it
is a process that stops responding while it allocates an arbitrary-precision integer, so the
exponent is capped. And `(-1) ** 0.5` returns a complex number while `1e308 * 10` returns `inf`,
both silently — so the result is checked for being a finite real number before it is handed back
as an answer.

The general rule this instantiates: input from a model is untrusted input. Allow-list what you
understand, reject the rest by default, and never let the model's good behaviour be the thing
standing between an attacker and your process.

---

### Q18 — `now` is READ_ONLY, but re-running it returns a different answer. Is that not a contradiction?

It looks like one only if READ_ONLY is read as "deterministic". It means **safe to re-execute** —
that replaying the call does not change the world. `now` has no side effects at all, so replaying
it is free, and D-004 resolves a READ_ONLY tool found PENDING after a crash by simply running it
again.

The wrinkle is real though: a resumed run sees time jump forward between the pre-crash attempt
and the replay. That is fine for this tool because nothing downstream requires the two calls to
agree. It would *not* be fine for a tool whose correctness depended on returning the same value
twice — a token generator, a random sample used to partition data. Those need the recorded result
replayed from the ledger instead of being re-executed, which is a different mechanism from the
effect class and is worth not conflating.

The short version: the effect class answers "what does replay do to the world?" It does not
answer "does replay return the same value?" Only one of those questions is about safety.

---

## v0.8 — the real adapter · 2026-09-19

---

### Q19 — You carry unknown content blocks through untouched but fail on an unknown stop_reason. Why not treat them the same way?

Because they are opposite risks, so being consistent would be the wrong goal.

A block type I do not recognise is one I never act on — I only replay it — and replaying it
unchanged is exactly the correct behaviour. Failing on it would turn a new block type in a future
API version into an outage, for a value that was never going to be read anyway.

A `stop_reason` is the single field the loop branches on. Refusal handling, truncation handling,
"is this an answer or a tool call" — all of it hangs off that one value. If a new terminal
condition fell silently into the `end_turn` branch, the run would return a partial response as a
finished answer, confidently, with no error anywhere. That is the worst failure available: wrong,
silent, and delivered as success.

So the rule is: liberal in what you carry, strict in what you branch on. Failing loudly means the
new value gets added to the literal by someone who has decided what the loop should do about it.
(D-019.)

---

### Q20 — What actually forced the adapter to exist, rather than just calling the SDK from the loop?

Four things live in the adapter and none of them belong in the loop.

**Which model.** The client is bound to one model at construction, so the eval harness's cheap
judge is a second client rather than a parameter threaded through the loop.

**Prompt caching.** `core` hands over a plain system string; the `cache_control` breakpoint is
attached in the adapter, on the system block — which covers the tool schemas too, because render
order is tools, then system, then messages, and a breakpoint caches everything up to and
including its own block. Caching is a property of the transport, not of the conversation.

**The retryable/non-retryable split.** The adapter maps `RateLimitError`, `APIConnectionError`
and a 5xx `APIStatusError` to `LLMTransportError`, and everything else to `LLMRequestError`. That
is what lets the loop implement a retry budget without importing `anthropic` and without knowing
`RateLimitError` exists. Collapsing them into one broad catch would erase the only distinction
that justifies catching at all.

**Translation.** Our own message types to the SDK's and back (D-014).

The test of whether the boundary is real is the v0 exit criterion: the same `AgentLoop` object
completes a run against the real API with one line changed — `FakeLLM` becomes
`AnthropicClient`. If that swap had needed a single edit inside `core/`, the boundary would be
decorative.

---

## v1.1-1.3 — the durability schema, the store, and replay · 2026-09-19

---

### Q21 — Why are steps and messages two tables rather than one?

Because they answer different questions and have different consumers.

A step is an **accounting record** — what it cost, how many attempts it took, what stopped it. It
is what the audit trail and the cost dashboard read. A message is **payload** that has to go back
to the model byte for byte. A resume needs the messages and does not care about the accounting;
the billing view needs the accounting and must not have to parse conversation blocks to get it.

They also do not map one to one. A single step produces two messages — the assistant turn, then
the user message carrying all the tool results — and the opening user message belongs to no step
at all. Forcing them into one table would mean either a nullable half of every row or a message
column that sometimes holds two messages.

---

### Q22 — `next_step_idx` is `len(self.steps)`. Why not keep a counter column and read that?

Because a counter and the rows it counts are two facts that can disagree, and after a crash you
cannot tell which one is right.

If the counter is incremented in the same transaction as the step insert then it is redundant —
it can never say anything the rows do not already say. If it is incremented anywhere else, then
there is an interleaving where the process dies between the two writes and the run permanently
believes it is at a step it never committed. Deriving the index from the rows means the rows are
the single truth, and `UNIQUE(run_id, idx)` is what makes that truth enforceable: a replayed step
cannot become a second row, it raises.

The same argument applies to the message index, which is counted rather than tracked.

`runs.steps_taken` does exist, but only as a denormalised read convenience for listing runs
without joining — the resume path never trusts it.

---

### Q23 — What exactly does "byte-identical replay" mean here, and what breaks without it?

It means the conversation loaded out of Postgres serialises to the same JSON the model originally
sent. Blocks are stored as JSONB exactly as they arrived, and rebuilt through `block_from_dict` —
the same function the provider adapter uses on a live response, so there is one implementation
and no second chance to disagree.

Two things break without it, and neither raises. First, a thinking block that comes back altered
invalidates the turn — the model does not reject it, it just behaves worse. Second, the
conversation prefix is the prompt-cache key, so a block that does not reproduce exactly stops the
cache matching and the symptom is the bill rather than a traceback.

The reason it is cheap to get right is D-014: blocks the loop does not interpret are never parsed
into fields, so there is no rendering step in which to lose a `signature`. The integration test
asserts the signature survives a real round trip through the database, because that is the field
whose loss is most silent.

---

## v1.4, v1.6 — leases and the idempotency ledger · 2026-09-19

---

### Q24 — Walk me through the crash window. Where exactly can you lose, and what do you do about it?

Four points, three gaps:

    t0  ledger row written PENDING, committed
    t1  the tool executes            <- the side effect, outside my database
    t2  ledger completed with the result, committed
    t3  step row committed

**Crash between t0 and t1.** PENDING, and the tool definitely did not run. I cannot distinguish
this from the next case, which is why it gets no separate treatment.

**Crash between t1 and t2.** PENDING, and the tool may or may not have run. This is the window
that cannot be closed — the side effect landed outside Postgres, so no transaction spans it and
no amount of cleverness recovers the information. It is made *safe* rather than closed: every
tool declares an effect class at definition time and that decides. READ_ONLY and
IDEMPOTENT_WRITE re-execute; UNSAFE stops the run for a human.

**Crash between t2 and t3.** SUCCEEDED with the result recorded, but the step was never
committed. The resumed run redoes the step, the ledger recognises the call, and it returns the
recorded result *without executing the tool again*. This window is fully closed, and closing it
is why `complete()` commits on its own rather than inside the step transaction (D-021).

So the honest claim is not exactly-once. It is at-least-once delivery with effectively-once
outcomes, for tools whose author has said replay is acceptable — and a hard stop for those who
have not.

---

### Q25 — Why is `tool_use_id` not part of the idempotency key?

Because the provider generates a fresh one on every response. Putting it in the key would make
the key different on every replay, so the ledger would never match anything — and the failure
would be invisible: every mechanism would run, every row would be written, and nothing would ever
be deduplicated. A broken safety mechanism that looks exactly like a working one.

The key is `hash(run_id, step_idx, tool_name, args)`. `run_id` and `step_idx` scope it to one
position in one run. `tool_name` and `args` mean a resumed step that produces the same call
matches the record, while a step where the model changes its mind produces a different key and
genuinely re-executes — which is correct, because it *is* a different call.

One detail that is easy to miss: the arguments are serialised with `sort_keys=True`. Python dicts
preserve insertion order and the model emits JSON in whatever order it likes, so without canonical
serialisation the same call could hash two ways, with the same silent consequence.

---

### Q26 — You claim a lease with a single UPDATE rather than SELECT ... FOR UPDATE. Which is right?

Both, for different jobs, and the distinction is the interesting part.

For a **known run id** it is a single conditional UPDATE — set the owner where the id matches and
the lease is either null or expired, returning the id. The row lock is taken by the UPDATE
itself, so there is no window between deciding the lease is free and taking it. A SELECT followed
by an UPDATE has that window, and under concurrency several workers pass through it together:
the lost-update bug. There is a test that races twenty workers at one run and asserts exactly one
winner.

For **finding work** it is `SELECT ... ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1` inside
the claiming transaction. The problem there is different: without SKIP LOCKED every polling
worker blocks on the same first row and the pool serialises — ten workers delivering one
worker's throughput. SKIP LOCKED makes each worker step over what others have locked.

The other thing worth saying: every expiry comparison uses `now()` — the *database's* clock,
never the worker's. Two workers with a few seconds of skew would otherwise disagree about whether
a lease had expired, and both would believe they owned the run.

---

## Questions I still owe answers to

Open, to be answered as the phases land. Written down now so they are not quietly avoided.

- What is the actual p95 cost of a step checkpoint, and does it hold the 15ms budget under
  concurrent runs?
- At what SSE connection count does the per-connection `XREAD` design actually fall over?
- Does the cross-encoder earn its latency on *my* corpus, or is hybrid-without-rerank the better
  operating point?
- What is the real failure mode when two workers race a lease — does `SKIP LOCKED` behave as
  expected under contention, or is there a starvation case?
- How much does prompt caching actually save across a multi-step run, in rupees?
