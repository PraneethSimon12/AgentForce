# AgentForge — Product Spec

**Governs behaviour.** When this document and any other disagree, this one wins and the other
gets corrected. Design lives in `architecture.md`; the wire format lives in `plan.md`.

---

## 1. What it is, in one paragraph

AgentForge takes a question and a corpus, and runs an LLM agent that plans, searches, calls tools,
and answers with citations — as a **durable background job** rather than a blocking HTTP request.
The caller watches it happen over a stream. If the server dies mid-run, the run continues on
another worker from its last completed step, without repeating side effects.

## 2. Who it is for

**The realistic user is a developer** — this is infrastructure, not a product. Three roles show up
in the requirements:

| Role | Wants | Cares most about |
| --- | --- | --- |
| **API consumer** | Ask a question, watch the answer stream, get citations | Latency to first token; trustworthy citations |
| **Operator** (me) | See what a run did and why it cost what it cost | The step-by-step audit trail; token spend |
| **Evaluator** (CI) | A number for retrieval and answer quality, repeatably | Determinism; ablations |

The evaluator being a first-class user is the unusual part, and it is deliberate: it is what
forces the retrieval pipeline to stay measurable instead of becoming a pile of heuristics.

## 3. Core journeys

### J1 — Ask a question and watch it answered

1. Client `POST /v1/runs` with an agent name and a question. Gets a `run_id` back in
   milliseconds; the run has been *recorded*, not started.
2. Client opens the SSE stream. Within ~1.5s (N1) the first token arrives.
3. The agent calls `retrieve`, gets chunks, may search again with a refined query, then answers.
   The client sees `tool.call`, `tool.result`, `token` and `citation` events as they happen.
4. `run.completed` carries the final answer plus resolved citations.

**The success criterion is not "it answered".** It is that the user could tell what the agent was
*doing* while it did it. An agent that is silent for ninety seconds and then produces a perfect
answer has failed this journey.

### J2 — The server dies mid-run

1. A run is on step 5 of 8; a `DURABLE` tool is executing on a Celery worker.
2. The worker is `SIGKILL`ed. Nothing is cleanly shut down; nothing gets a chance to write.
3. The run's lease expires. Another worker claims it, reads the committed steps, rebuilds the
   message list, and continues.
4. The tool that was in flight is resolved by its ledger entry and effect class: re-executed if
   safe, or the run stops for review if not.
5. The client's SSE connection dropped when the process died. It reconnects with `Last-Event-ID`
   and receives the events it missed, then continues live.

**Success:** the run completes, the tool's side effect happened **exactly once**, and the user saw
a pause — not an error, not a duplicate, not a truncated answer.

### J3 — Ingest a corpus and query it

1. `POST /v1/documents` with text. Returns immediately; chunking and embedding are background
   work. Re-posting the same content is a no-op by content hash.
2. Once ingested, chunks are retrievable by the `retrieve` tool and by `POST /v1/search`.
3. `/v1/search` with `explain: true` shows each retriever's rank, the fused rank, the rerank
   score, and per-stage timings — so retrieval behaviour is inspectable per query, not inferred.

### J4 — Delegate to a sub-agent

1. A supervisor agent decides a sub-task needs its own investigation and calls `delegate`.
2. A child run starts with `parent_run_id` set, **a slice of the parent's remaining budget**, and
   its own tool allowlist.
3. The child's intermediate work never enters the parent's context — only its conclusion does,
   returned as a tool result.
4. The client can stream the child run separately via `delegate.started`.

**Success:** the parent's context stays small, and the total cost of parent + children never
exceeds the parent's original budget.

### J5 — CI catches a quality regression

1. A prompt or retrieval change is merged to `main`.
2. CI runs the golden set: retrieval metrics, then judged answer quality.
3. A drop beyond threshold fails the build, and the report names which metric moved and by how
   much.

**Success:** a deliberately-worsened prompt fails the build. An eval gate that has never failed is
not known to work, so this is verified on purpose at least once (plan v4 exit).

## 4. Behaviour at the edges

These are the answers that must not be improvised later. Each is a behavioural commitment.

| Situation | Required behaviour |
| --- | --- |
| Model asks for a tool that does not exist | Return a `tool_result` with `is_error: True` naming the valid tools. The model gets to correct itself; the run does not die. |
| Model sends arguments that fail validation | Same: the `ValidationError` goes back as an error result. Two consecutive failures on the same tool ends the step. |
| A tool raises | Error result to the model, step marked `ERROR`, retry only within the step's bounded allowance. |
| A tool exceeds its timeout | Step is `TIMEOUT`. The model is told the tool timed out, so it can try a different approach. |
| Step cap or token budget reached | Terminal `BUDGET_EXCEEDED`, with whatever partial output exists. Never silently truncated, never quietly extended. |
| Model returns `stop_reason: "refusal"` | Terminal, with the refusal category surfaced. Not a crash, not a retry. |
| Client disconnects | The run **continues**. It is a job, not a request. The client may reconnect or read the result later. |
| Client reconnects after 10 minutes | Replay only if events are still within stream retention; otherwise the current state is returned and the client is told replay was unavailable. Silent partial replay is worse than an honest gap. |
| Cancel during a 30-second tool | Takes effect at the next step boundary. The response says so — it does not pretend the run stopped instantly. |
| Two workers pick up the same run | Exactly one wins the lease. The other declines; it does not wait, and it does not run a second copy. |
| Redis is down | Runs continue; streaming degrades. History and results are intact. |
| Postgres is down | Runs stop. This is the one hard dependency, by design. |
| Corpus is empty | `retrieve` returns nothing and the agent says it cannot answer from the corpus. It must not answer from parametric memory and dress it up with citations. |
| Model cites a chunk it was never given | **Detected and flagged**, because citations are IDs resolved against what was actually retrieved. This is a validation failure, not a matter of taste. |
| Same document ingested twice | No-op by `sha256`. Duplicate chunks would silently corrupt recall metrics. |

## 5. What it does not do

- **No human-in-the-loop approval UI.** `UNSAFE` tools stop the run for review; the review itself
  happens out of band. The hook exists; the interface does not.
- **No authentication or multi-user accounts.** `tenant_id` is a column and a filter, not a login.
- **No agent that acts on the outside world.** Read-only tools and writes to our own database
  only, until approval gating is built and tested.
- **No chat memory across runs.** Each run is independent. Threading is a schema change, not a
  redesign, and it is not in scope.
- **No model choice per request.** One model, configured. Per-request model selection invalidates
  the prompt cache and complicates evals for no learning.
- **No streaming of the corpus, no live document sync, no connectors.** Ingestion is an API call.

## 6. Acceptance — the four demos

The project is "done enough to talk about" when these four can be run on demand, in this order:

1. **The loop.** A question that needs two tool calls, answered with the step trail visible.
2. **The crash.** `kill -9` mid-run; the run finishes correctly and the tool ran exactly once.
   This is the one that is genuinely hard, and the one to lead with.
3. **The retrieval ablation.** A table showing dense-only vs lexical-only vs hybrid vs
   hybrid+rerank on the same golden set, with latency alongside quality.
4. **The gate.** Deliberately worsen a prompt, watch CI fail, revert, watch it pass.

If a demo needs a caveat spoken out loud to make sense, it is not finished.
