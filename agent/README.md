# Covenant compliance agent

An autonomous agent for the Halyk AI Challenge. It reads a directory of opaque
PDFs plus a transaction ledger and emits `submission.json` — a verdict, a
supporting number, and an evidence transaction for every borrower × covenant.

```bash
python agent/run.py --preflight     # check the API before committing to it
python agent/run.py                 # full run
python agent/run.py --no-llm        # deterministic core only
```

Cold start, including re-extracting 200 PDFs: **~12 seconds**.

## Architecture

```
                        documents/*.pdf
                              │
                              ▼
                  PRE-INDEX  (index_build.py, once)
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
      rules_index.json                   chunks.json
   borrower → account →              1158 retrievable passages,
   clause → {rule, rule_type,        each tagged with account_id
   threshold, direction,             and doc_type
   keywords, requires}
              │                               │
              │                               ▼
              │                         RAG (retrieve.py)
              │                    scope by borrower → embed →
              │                    cosine → rerank; BM25 fallback
              │                               │
master_ledger.csv                             │
      │                                       │
      ▼                                       │
 CLASSIFY (rules first)                       │
   description → category                     │
      │                                       │
      ├── unplaced rows ──► LLM categoriser ──┤
      │                                       │
      ▼                                       ▼
 ADJUST  reclassification · cut-off · FX · off-ledger · related party
      │
      ▼
 COMPUTE  Decimal arithmetic, one metric formula per clause
      │
      ▼
 EVIDENCE  counterfactual: revert one row, does the verdict flip?
      │
      ├──────────────────► LLM review pass ──► reviews.json (flags, never flips)
      ▼
 submission.json
```

## Where the LLM is, and where it is deliberately not

The LLM does three jobs, all classification or extraction:

1. **Categorise transactions the rule table could not place.** Rules run first and
   handle the known vocabulary; the model only sees leftovers.
2. **Map an unrecognised covenant onto a known metric formula.** Only fires when
   `metrics.LIBRARY` matched nothing — i.e. the graded archive used wording we
   have not seen.
3. **Review a finished verdict** against the clause text retrieved from the
   agreement, and say whether the *reading* is right.

It never does arithmetic, and never picks a status. Every number comes from
`Decimal` code in `pipeline/`, so a hallucinated digit cannot reach the answer —
the worst a bad call can do is mislabel one input, which the health checks flag.

A reviewer objecting **flags** a cell; it does not overturn verified arithmetic.
One model's unease is not evidence. If `LLM_MODEL_B` is set, a flagged cell gets a
second opinion from a different model, because two independent models objecting is
a much stronger signal than one.

## Why no vector database

1158 passages is a ~4 MB float array. A network round-trip to Milvus costs more
than the entire in-memory scan, and adds a service that can be down during a
three-hour submission window. Retrieval is also *scoped before it is scored* —
a query for borrower P1 only ever ranks P1's passages — which does more for
precision than any embedding model, because ~150 documents in the corpus are
decoys that name real borrowers.

## Failure behaviour

Designed around the submission window, not the happy path:

- **The deterministic core runs first** and produces a complete valid submission
  before the LLM is touched. If the API is slow, rate-limited or down, the run
  still ends with a file on disk.
- **Preflight** makes one cheap call and falls back to `--no-llm` if it fails,
  rather than discovering the problem 40 minutes in.
- **Every LLM call is disk-cached by content hash**, so a rerun costs nothing and
  a crash resumes instantly.
- **A hard call budget** (`LLM_BUDGET_CALLS`) means a loop cannot eat the window.
- **Emission writes before it validates.** A wrong cell and an empty cell score
  the same, but an unwritten file forfeits every other cell too.

## Configuration

Copy `.env.example` to `.env`. Nothing has a secret baked in. With no `.env` at
all the agent runs deterministic-only and still scores 34.000/36 on the public
archive.

## Outputs (`agent/work/`)

| File | Contents |
|---|---|
| `submission.json` | the deliverable |
| `rules_index.json` | the pre-index: every borrower, clause, rule, threshold, keywords |
| `chunks.json` | retrievable passages |
| `compute_report.json` | per-cell metric, comparison, evidence, verbatim clause |
| `reviews.json` | the review pass: agreement, concerns, cross-checks |
| `run_meta.json` | LLM call counts, cells flagged, elapsed time |

## Rules compliance

Contest rules allow assistant tooling for *development* but require the answers to
be generated by your own agent. Every value in `submission.json` is produced by
this code from the archive — no answer is hand-entered, and the ground-truth key
that ships in the public archive is used only by `pipeline/score.py` for
validation, never on the path that writes a submission.
