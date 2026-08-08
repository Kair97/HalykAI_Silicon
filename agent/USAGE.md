# How to run the agent

## Setup (done — verify only)

`agent/.env` is configured for Groq. `.env` is gitignored, so the key is not
committed. **Rotate the key at console.groq.com after the hackathon** — it was
pasted into a chat log.

```bash
python agent/run.py --preflight
# OK   LLM enabled @ https://api.groq.com/openai/v1  [chat=openai/gpt-oss-120b, cross-check=qwen/qwen3.6-27b]
```

If that prints `FAIL`, run everything below with `--no-llm`. You lose nothing on
known wording — the deterministic core scores the same.

## On submission day

```bash
# 1. drop the private archive in place of the public one
#    (must contain: documents/, master_ledger_2025.csv, submission_template.json)
mv ~/Downloads/private-archive  agentic-bank-public

# 2. run
python agent/run.py

# 3. the deliverable
cat agent/work/submission.json
```

**Runtime:** ~12 s deterministic, ~7–20 min with the LLM review pass (Groq's free
tier rate-limits long prompts, so the review pass is the slow part).

If the clock is tight, `python agent/run.py --no-llm` gives you a complete,
validated submission in 12 seconds. Do that first, keep the file, then run the
full version if time allows.

## Reading the output

The run ends with a health report. **Read it before submitting.**

```
--- health: 0 FAIL, 2 WARN
  [WARN] P2: no legible KYC ownership table; related parties resolved by fallback
```

- `FAIL` — that cell's inputs are broken. The number is not trustworthy. The
  submission is still written (a wrong cell and an empty cell score the same, but
  a missing file forfeits everything).
- `WARN` — degraded but defensible. Worth a look, not a blocker.
- A non-zero exit code means "written, but N cells are flagged" — **not** "nothing
  was produced".

Other artefacts in `agent/work/`:

| File | Use |
|---|---|
| `submission.json` | the deliverable |
| `rules_index.json` | every borrower → clause → rule, threshold, keywords |
| `compute_report.json` | per cell: formula, unrounded value, comparison, evidence |
| `reviews.json` | the LLM review pass (advisory — see below) |
| `run_meta.json` | call counts, failures, cells flagged, elapsed seconds |

## What to do if something breaks

| Symptom | Cause | Fix |
|---|---|---|
| `FAIL … no live credit agreement` | triage lost a document | check `index.json`; the archive may use different header markers |
| `FAIL … category X matched nothing` | new description vocabulary | add a pattern to `RULES` in `pipeline/p3_categorise.py` |
| `UnmatchedCovenant` | new covenant wording | the LLM resolver handles it; with `--no-llm`, add an entry to `metrics.LIBRARY` |
| `WARN … decoy filter retained N rows` | different ledger shape | usually harmless; check a few rows by hand |
| Everything fails | API down | `--no-llm` |

## Honest note on the review pass

Measured on the public archive: it reviewed 27–32 cells per run and flagged 6,
of which **1 was a genuine problem** (`P9/6.1`, a known corpus gap) and 5 were
false alarms. It also missed two cells that really are wrong.

So treat `reviews.json` as **a list of things to glance at, not a verdict**. It
never changes an answer — arithmetic that has been verified is not overturned by
one model's unease. Its concerns also shift between runs, which is itself a
signal that they are weakly grounded.

If you are short on time on submission day, `--no-llm` costs you nothing
measurable and saves 7–20 minutes.

## Where the LLM actually earns its place

Not on the public archive — the rules already cover its vocabulary, so the LLM
relabelled **0 rows**. It matters when the private archive uses wording the rule
table has never seen. Measured on synthetic drift tests: at 25% unseen
vocabulary the deterministic core drops to 69.7%, and that is exactly the gap the
LLM categoriser is there to close.
