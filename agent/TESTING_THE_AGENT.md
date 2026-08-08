# How to test the agent — step by step

Everything below is copy-paste. Run from `~/Desktop/HalykAI`.

---

## Step 0 — Is the API alive?

```bash
python agent/run.py --preflight
```

Expected:
```
OK   LLM enabled @ https://api.groq.com/openai/v1  [chat=openai/gpt-oss-120b, cross-check=qwen/qwen3.6-27b]
```

If it says `FAIL`, add `--no-llm` to every command below. You lose nothing on
known wording.

---

## Step 1 — One run, one numbered attempt

```bash
python agent/run.py --no-llm
```

Every run creates its own folder so nothing is ever overwritten:

```
agent/work/runs/try1/
├── submission.json          ← the deliverable (template shape — what you upload)
├── answers_key_shape.json   ← same answers in ground_truth.json shape (for diffing)
├── compute_report.json      ← per cell: formula, unrounded value, comparison, evidence
├── reviews.json             ← LLM review pass output
└── run_meta.json            ← calls, failures, cells flagged, seconds
```

The next run becomes `try2`, then `try3`, and so on. To overwrite a specific one:
`--try-number 4`.

**Note on the two formats.** `submission.json` uses the *template* shape
(`answers.P1["6.1"]`) — that is what the contest requires, and the agent reads
`submission_template.json` to learn which borrowers and clauses exist. It never
assumes `P1..P10/B1/B4`. `answers_key_shape.json` is the same content in
`ground_truth.json` shape (`scenarios.P1.covenants["6.1"]`) purely so you can diff
against a key.

---

## Step 2 — The three run modes

```bash
python agent/run.py --no-llm       # deterministic only          ~12 s cold, 0.5 s warm
python agent/run.py --no-review    # + LLM categoriser            ~1 min
python agent/run.py                # + LLM review pass            ~7-20 min
```

The middle one is usually the right default: the categoriser is where the LLM
actually earns points (unseen vocabulary), and the review pass is advisory and
slow because Groq's free tier rate-limits long prompts.

---

## Step 3 — Compare attempts

```bash
python agent/compare.py
```

```
try      llm  review  calls  flag    secs  statuses            score
------------------------------------------------------------------------------
try1      no      no      0     0     0.5   35/36     34.000/36
try2     yes      no      0     0     0.4   35/36     34.000/36

imperfect cells in the latest attempt:
  P4/6.1    ACTUAL  0.37 vs 0.33 (12.1%)
  P4/6.3    STATUS  BREACH != COMPLIANT
  P9/6.1    ACTUAL  0.37 vs 0.22 (68.2%)
```

Cell-by-cell diff between two attempts:

```bash
python agent/compare.py try1 try3
```

This is the one that still works on the **private** dataset, where there is no key:
it shows exactly which cells changed between attempts, so you can see whether the
LLM layer moved anything and whether the movement looks sane.

---

## Step 4 — Does it work on data it has never seen?

This is the important test. The public archive is the data every rule was written
while looking at, so scoring well on it proves less than it appears to.

```bash
# 10 freshly generated archives — new borrowers, thresholds, traps
python tests/benchmark.py --seeds 10
```

```
status accuracy : 100.00%   (360 of 360 cells)
weighted score  : 100.00%
```

```bash
# what happens when the wording drifts away from what the rules know
python tests/benchmark.py --seeds 3 --drift 0.25
```

| drift | status accuracy | weighted score |
|---|---|---|
| 0% | 100.0% | 100.0% |
| 25% | 79.6% | 69.7% |
| 100% | 57.4% | 32.4% |

That drift row is exactly the gap the LLM categoriser exists to close.

```bash
# pass/fail gate: synthetic archive + no regression on the real one
python tests/test_e2e.py
```

---

## Step 5 — Read the health report before submitting

Every run ends with one:

```
--- health: 0 FAIL, 2 WARN
  [WARN] P2: no legible KYC ownership table; related parties resolved by fallback
```

- **FAIL** — that cell's inputs are broken; the number is not trustworthy. The
  submission is still written (a wrong cell and an empty cell score the same, but
  a missing file forfeits everything).
- **WARN** — degraded but defensible.
- A non-zero exit code means *"written, but N cells are flagged"* — not *"nothing
  was produced"*.

---

## Submission day, in order

```bash
# 1. put the private archive where the public one is
mv ~/Downloads/<private-archive>  agentic-bank-public

# 2. FIRST: guaranteed file in 12 seconds
python agent/run.py --no-llm --try-number 1

# 3. then the better one, if the clock allows
python agent/run.py --no-review --try-number 2

# 4. see what moved
python agent/compare.py
python agent/compare.py try1 try2

# 5. upload the one you trust
cat agent/work/runs/try2/submission.json
```

There will be **no `ground_truth.json`** in the private archive, so `compare.py`
shows changed-cell counts instead of scores. That is expected — the agent prints
`(no ground_truth.json in the archive -- nothing to score against)`.

---

## What "good" looks like

| Signal | Good | Investigate |
|---|---|---|
| health report | `0 FAIL` | any `FAIL` names the untrustworthy cells |
| cells emitted | all of them | a missing key is a scored zero |
| `try1` vs `try2` diff | 0–3 cells moved | many cells moving means the LLM is overriding sound rules |
| `run_meta.llm_calls.failed` | 0–2 | high means rate limiting; rerun, the cache makes it cheap |
| `rows_relabelled_by_llm` | 0 on known wording | high on the private set is expected and good |

Every LLM call is cached on disk by content hash, so re-running after a crash costs
nothing and resumes instantly.
