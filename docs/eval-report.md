# Eval & Performance Report — AgentForge

**This file is the evidence.** `resume-claims.md` is the claim. Nothing moves from here to there
until a row below is filled in by a real run.

Every entry records: the number, the **configuration that produced it**, the commit, and the date.
A number without its configuration is not reproducible, and a number that is not reproducible is
not evidence. `ef_search`, chunk size, `top_k`, rerank depth and prompt version all change the
result, so all of them get written down.

**Status: empty. No runs yet — the project is pre-v0.** Every table below is a template with its
columns fixed in advance, deliberately: deciding what to measure *before* measuring removes the
temptation to report whichever number happened to look good.

---

## 1. Retrieval quality — the ablation table

The headline table. It is what makes "hybrid RAG improved quality" a claim rather than an
assertion, and it is the direct evidence for resume bullet 3.

**Golden set:** 50 questions over the QueueFair + AgentForge docs corpus, each with hand-labelled
relevant `chunk_id`s. A held-out slice of 15 is reserved from day one and never used for tuning.

| Pipeline | recall@10 | nDCG@10 | MRR | p50 ms | p95 ms | Commit | Date |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Dense only (baseline) | — | — | — | — | — | — | — |
| Lexical only | — | — | — | — | — | — | — |
| Hybrid (RRF, k=60) | — | — | — | — | — | — | — |
| Hybrid + cross-encoder rerank | — | — | — | — | — | — | — |

**Config for the above:** embedding `bge-small-en-v1.5` (384d) · HNSW `m`=?, `ef_construction`=?,
`ef_search`=? · chunk size ? tokens, overlap ? · `top_k`=50 per retriever · rerank top-8 ·
corpus size ? chunks.

**Read the latency column as carefully as the quality column.** If rerank buys +0.10 nDCG for
+250ms p95, that is a real trade with a real answer — and reporting only the quality half is how
a portfolio project quietly lies.

## 2. Chunking sweep

Chunking moves retrieval quality more than the embedding model does, and it is the cheapest knob.

| Chunk tokens | Overlap | Chunks | recall@10 | nDCG@10 | Notes |
| --- | --- | --- | --- | --- | --- |
| 256 | 0 | — | — | — | — |
| 512 | 64 | — | — | — | — |
| 1024 | 128 | — | — | — | — |

## 3. Answer quality — LLM-as-judge

Judged on the same golden set. **Judge configuration is part of the result** — a different judge
model or rubric version makes the numbers incomparable, so both are recorded per run.

| Metric | Score /5 | n | Judge model | Rubric ver | Prompt ver | Commit | Date |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Faithfulness (claims supported by retrieved chunks) | — | — | — | — | — | — | — |
| Citation validity (cited IDs exist and support the sentence) | — | — | — | — | — | — | — |
| Completeness vs reference answer | — | — | — | — | — | — | — |

**Known biases, stated with the numbers, not in a footnote:** position bias (mitigated by
randomised order), self-preference bias (mitigated by judging with a different model tier), and
small-n variance — 50 questions is a smoke test, not a guarantee.

**Deterministic metrics outrank the judge.** Where §1 can answer the question, §1 answers it.

## 4. Runtime performance

| Metric | Target (arch §1) | Measured | Conditions | Commit | Date |
| --- | --- | --- | --- | --- | --- |
| Time to first token, p95 | < 1.5s (N1) | — | — | — | — |
| Step checkpoint overhead, p95 | < 15ms (N2) | — | — | — | — |
| Resume-after-crash | < 2s (N3) | — | — | — | — |
| Retrieval end-to-end, p95 @100k chunks | < 400ms (N4) | — | — | — | — |
| Concurrent runs before degradation | — | — | — | — | — |
| Concurrent SSE connections before degradation | — | — | — | — | — |

## 5. Durability — the chaos results

**The most valuable table in this file.** N5 is binary: zero duplicate side effects, or the claim
is false.

| Scenario | Runs | Completed | Duplicate effects | Mean resume | Commit | Date |
| --- | --- | --- | --- | --- | --- | --- |
| `kill -9` worker mid-`DURABLE`-tool | — | — | **must be 0** | — | — | — |
| `kill -9` during the LLM call | — | — | **must be 0** | — | — | — |
| `kill -9` between tool completion and step commit | — | — | **must be 0** | — | — | — |
| Two workers racing one run | — | — | **must be 0** | — | — | — |
| `UNSAFE` tool interrupted | — | — | n/a — must stop for review, never auto-retry | — | — | — |

Row 3 is the interesting one: it is the t1–t3 window from `architecture.md` §6, and the only
reason it passes is the ledger-plus-effect-class design. If it ever shows a non-zero count, the
durability claim comes off the CV that day.

## 6. Cost

Tracked because CLAUDE.md §7 is a hard limit, and because an unmeasured limit is a wish.

| Item | Tokens in | Tokens out | Cache read | ₹ est. | Notes |
| --- | --- | --- | --- | --- | --- |
| Typical single-agent RAG run | — | — | — | — | — |
| Supervisor + 2 sub-agents | — | — | — | — | Must not exceed the parent's budget |
| Full eval run (50 q × 4 pipelines + judge) | — | — | — | — | The biggest line item |
| Monthly actual | — | — | — | — | Ceiling ₹2,000, target ₹1,000 |

**Cache read tokens are a first-class metric.** If `cache_read_input_tokens` is ~0 across repeated
runs, something is invalidating the prefix and the bill will say so before anything else does.

---

## Run log

Newest first. One entry per meaningful run: what changed, what moved, what surprised me.
The surprises are the point — a run that only confirms expectations teaches nothing, and the
entries worth re-reading later are the ones where the number went the wrong way.

_(empty)_
