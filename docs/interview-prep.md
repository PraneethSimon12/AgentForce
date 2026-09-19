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
