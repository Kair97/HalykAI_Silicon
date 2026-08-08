# Covenant Compliance Agent — complete guide

Everything needed to run, understand, test and extend this agent, assuming no
prior knowledge of it.

---

## Table of contents

1. [What this agent does](#1-what-this-agent-does)
2. [Setup from zero](#2-setup-from-zero)
3. [Quick start](#3-quick-start)
4. [How it works](#4-how-it-works)
5. [Command reference](#5-command-reference)
6. [What it writes](#6-what-it-writes)
7. [Submission day: the private dataset](#7-submission-day-the-private-dataset)
8. [When the private data is different](#8-when-the-private-data-is-different)
9. [Testing it yourself](#9-testing-it-yourself)
10. [Known limitations](#10-known-limitations)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. What this agent does

**The problem.** A bank lends money under contracts containing *financial
covenants* — promises such as "capital expenditure must not exceed 42% of
operating costs plus lease payments" or "revenue must not fall below $7,100,000".
The bank needs to know which borrowers broke which promises.

**The inputs** are deliberately messy:

| Input | Reality |
|---|---|
| `documents/` | ~200 PDFs with meaningless hash filenames (`8d878af064f2.pdf`). You cannot tell what a document is, or whose it is, without opening it. Contains real loan agreements, **superseded editions with different thresholds**, KYC dossiers, auditor notes, and ~150 pure decoys that name real borrowers. |
| `master_ledger_2025.csv` | ~1,473 transactions for **all** accounts in one table. No category column. Expenses negative. Mixed currencies. Only ~12 accounts matter; the rest is noise. |
| `submission_template.json` | Exactly which borrowers × covenants to answer. |

**The output** is one JSON: for each borrower × covenant, three fields.

| Field | Meaning | Weight |
|---|---|---|
| `status` | `COMPLIANT` or `BREACH` | 0.50 |
| `actual` | the real value of the constrained metric, positive, 2 dp | 0.30 |
| `evidence_txn_id` | the single transaction that *decides* the verdict, else `null` | 0.20 |

A wrong `status` zeroes the whole cell. `actual` is scored on a sliding scale that
reaches zero at 5% relative error — so precision genuinely matters.

**Current measured result:** 34.5 / 36 on the public archive (95.8%), 35/36
statuses correct, 33 cells perfect. See [§10](#10-known-limitations) for the two
that are not.

---

## 2. Setup from zero

### Requirements

| Thing | Why | Check |
|---|---|---|
| Python 3.11+ | runs everything | `python --version` |
| `pdftotext` (poppler) | PDF → text | `pdftotext -v` |
| A Groq API key *(optional)* | the LLM layer | — |

No `pip install`. The agent uses only the Python standard library, on purpose:
nothing to install at 1 a.m. when a deadline is close.

**If `pdftotext` is missing:**
- Windows: it ships with Git Bash at `/mingw64/bin/pdftotext.exe`; otherwise
  download poppler and put its `bin` on `PATH`
- macOS: `brew install poppler`
- Linux: `sudo apt install poppler-utils`

### Layout

```
HalykAI/
├── agentic-bank-public/       <- the dataset (swap this for the private one)
│   ├── documents/*.pdf
│   ├── master_ledger_2025.csv
│   ├── submission_template.json
│   └── ground_truth.json      <- answer key; present in public, ABSENT in private
├── pipeline/                  <- deterministic engine (the arithmetic)
├── agent/                     <- the agent: LLM, RAG, CLI, reports
│   ├── .env                   <- your API key (gitignored)
│   ├── runs/                  <- one directory per attempt
│   └── work/                  <- caches and intermediates
├── txt/                       <- cached pdftotext output (regenerates itself)
└── HOWTO.md                   <- this file
```

### Configure the LLM (optional)

Create `agent/.env`:

```bash
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_API_KEY=gsk_your_key_here
LLM_MODEL=openai/gpt-oss-120b
LLM_MODEL_B=qwen/qwen3.6-27b      # optional second opinion
LLM_TEMPERATURE=0
LLM_BUDGET_CALLS=600              # hard ceiling on API calls
TEAM_NAME=KOZ
CONTACT_EMAIL=you@example.com
```

`.env` is gitignored. **Never commit it.**

Without an `.env` the agent runs deterministic-only and still scores 34.5/36 on
the public archive.

> Groq has no embeddings or rerank endpoint. Retrieval automatically falls back to
> BM25, which needs no API at all. This is expected, not a failure.

### Verify

```bash
python -m agent check
```

```
archive : .../agentic-bank-public
  OK       submission_template.json     (12 borrowers, 36 cells: P1, P2, P3 ...)
  OK       master_ledger_2025.csv
  OK       documents                    (200 pdfs)
  OK       ground_truth.json            answer key (absent on the private set -- expected)

LLM     : OK   chat=openai/gpt-oss-120b, cross-check=qwen/qwen3.6-27b
          15 models reachable

ready
```

---

## 3. Quick start

```bash
cd HalykAI

# 1. is everything reachable?
python -m agent check

# 2. answer the covenants
python -m agent solve agentic-bank-public/submission_template.json --no-llm

# 3. look at how it decided
open agent/runs/try1/report.html      # macOS
start agent/runs/try1/report.html     # Windows
```

That's the whole loop. Runtime: **~12 s cold** (extracting 200 PDFs), **<1 s warm**.

---

## 4. How it works

```
              submission_template.json          documents/*.pdf      master_ledger.csv
                        │                             │                      │
                        │  which borrowers            │  pdftotext -layout   │
                        │  and clauses to answer      ▼                      │
                        │                          txt/*.txt                 │
                        │                             │                      │
                        ▼                             ▼                      ▼
              ┌──────────────────────────────────────────────────────────────────┐
              │ 1. TRIAGE      classify every document by header marker          │
              │                drop superseded editions and interim drafts       │
              │                resolve account_id -> scenario_id from the ledger │
              ├──────────────────────────────────────────────────────────────────┤
              │ 2. PARSE       Article 6 thresholds, KYC ownership tables,        │
              │                auditor notes, group accounts, treasury memos     │
              ├──────────────────────────────────────────────────────────────────┤
              │ 3. CATEGORISE  ledger rows -> revenue / capex / opex / labour /  │
              │                utilities / taxes / interest / insurance / lease  │
              │                  rules first, LLM only for wording rules miss    │
              ├──────────────────────────────────────────────────────────────────┤
              │ 4. ADJUST      auditor reclassifications, cut-off exclusions,     │
              │                FX settlement, amounts missing from the export,    │
              │                related-party identification from the KYC          │
              ├──────────────────────────────────────────────────────────────────┤
              │ 5. COMPUTE     Decimal arithmetic; formula selected from the      │
              │                covenant's own WORDING, not its clause number     │
              ├──────────────────────────────────────────────────────────────────┤
              │ 6. EVIDENCE    counterfactual: revert one row — does the verdict  │
              │                flip? if exactly one row flips it, that's evidence │
              ├──────────────────────────────────────────────────────────────────┤
              │ 7. EMIT        write submission.json, then validate, then report  │
              └──────────────────────────────────────────────────────────────────┘
                        │                    │                    │
                        ▼                    ▼                    ▼
                 verdicts.json         report.html          health report
```

### Five design decisions worth knowing

**1. `account_id` is the only borrower key.** Company names are reused across
decoy documents and do not map 1:1 to accounts — `ACC-7201` is Ekibastuz Energy
JSC, while other documents naming Ekibastuz Energy belong to different accounts.
Names are never used to link a document to a borrower.

**2. Clause numbers mean different things per borrower.** Clause 6.1 is an
interest-coverage ratio for one borrower, a Q4 revenue floor for another, a
capital-intensity cap for a third. The formula is chosen by matching the clause's
**verbatim wording** against a library of 19 metric shapes. A clause that matches
nothing raises `UnmatchedCovenant` rather than guessing.

**3. The LLM never does arithmetic.** It has exactly three jobs — categorise rows
the rules could not place, map unseen covenant wording to a formula, and review a
finished verdict. Every number comes from `Decimal` code, so a hallucinated digit
cannot reach the answer. A reviewer objection **flags** a cell; it never overturns
verified arithmetic.

**4. Description categorises, counterparty filters.** The transaction description
is the strong signal for *which category* a row belongs to. The counterparty tells
you whether the row is generated noise at all. Both stages matter.

**5. Write first, validate second.** A submission wrong in one cell still scores
the other 35; a submission that was never written scores zero everywhere. Problems
become loud warnings, not exceptions that kill the run.

---

## 5. Command reference

```bash
python -m agent check                       # verify archive + API
python -m agent solve <template> [options]  # the main command
python -m agent run [options]               # same engine, different entry
python -m agent compare [tryA tryB]         # score or diff attempts
python -m agent report [tryN]               # regenerate the HTML report
```

### `solve` — the one you'll use

```bash
python -m agent solve agentic-bank-public/submission_template.json
```

| Option | Effect |
|---|---|
| `--archive DIR` | where `documents/` and the ledger live (default: the template's own folder) |
| `-o FILE` | write verdicts here (default: inside the new run directory) |
| `--outdir DIR` | where run directories are created (default: `agent/runs`) |
| `--no-llm` | deterministic core only — fastest, needs no API |
| `--review` | also run the advisory LLM review pass (slow) |
| `--format key` | emit `ground_truth.json` shape instead of submission shape |

> **Which format do you submit?** The default (`submission`). It matches
> `submission_template.json` exactly — `answers.P1["6.1"]`. The `key` format
> (`scenarios.P1.covenants["6.1"]`) is only for diffing against an answer key.
> Submitting the wrong shape scores zero.

### Run modes and what they cost

| Mode | Time | When |
|---|---|---|
| `--no-llm` | ~12 s cold, <1 s warm | always run this first — guaranteed output |
| default | ~1 min | adds LLM categorisation of unknown wording |
| `--review` | 7–20 min | adds advisory review; slow because of API rate limits |

---

## 6. What it writes

Each `solve` creates `agent/runs/tryN/`:

| File | What it is |
|---|---|
| **`verdicts.json`** | **the deliverable — upload this** |
| `submission.json` | identical content, canonical name |
| `answers_key_shape.json` | same answers in `ground_truth.json` shape, for diffing |
| **`report.html`** | **the human-readable interface — open this** |
| `compute_report.json` | per cell: formula, unrounded value, comparison, evidence, every contributing row |
| `reviews.json` | LLM review pass output |
| `run_meta.json` | API calls, failures, cells flagged, elapsed seconds |

### Reading `report.html`

For every cell it shows the verdict, the number, the exact comparison, the
verbatim covenant text, and **the individual transactions that produced it**:

```
Clause 6.1  COMPLIANT  0.33   adjusted_ebitda_margin
test: 0.3314123065566434759783274103 >= 0.28 (minimum)

  TXN-P4-0054  Terminal servicing and operating costs 2025          opex  -4,431,662.19
  TXN-P4-0025  Flood remediation and silo repair works     ONE-OFF  opex    -481,247.63
  TXN-P4-0001  Berth silt cleaning and clearance works              opex    -251,338.94
  TXN-P4-0023  Demurrage dispute arbitration and legal…    ONE-OFF  opex    -342,905.28
  TXN-P4-0043  Grain terminal throughput sales settlement        revenue   7,004,318.47
  TXN-P4-0048  Management advisory retainer               RELATED consulting -288,417.52
```

Row tags: `RELATED` (counts as a related party), `EXCLUDED` (removed by an auditor
cut-off note), `ONE-OFF` (added back to EBITDA), `RECLASSIFIED` (the auditor moved
it to a different category).

### The health report — read this before submitting

Every run ends with one:

```
--- health: 0 FAIL, 2 WARN
  [WARN] P2: no legible KYC ownership table; related parties resolved by fallback
```

| Level | Meaning | Action |
|---|---|---|
| `FAIL` | that cell's inputs are broken; **the number is not trustworthy** | investigate before submitting |
| `WARN` | degraded but defensible | glance at it |
| non-zero exit | *"written, but N cells are flagged"* — **not** "nothing produced" | read the report |

---

## 7. Submission day: the private dataset

### Step by step

```bash
# 1. Put the private archive where the public one is.
#    Keep the public one — you may want to compare.
mv agentic-bank-public agentic-bank-public-OLD
mv ~/Downloads/<private-archive>  agentic-bank-public

# 2. Clear the cached PDF text — it belongs to the OLD documents.
rm -rf txt/ agent/work/

# 3. Verify before committing time to it.
python -m agent check

# 4. FIRST RUN: guaranteed output in ~12 seconds. Do this before anything else.
python -m agent solve agentic-bank-public/submission_template.json --no-llm

# 5. Read the health report and the HTML report.
start agent/runs/try1/report.html

# 6. If time allows, the LLM run.
python -m agent solve agentic-bank-public/submission_template.json

# 7. What changed between them?
python -m agent compare
python -m agent compare try1 try2

# 8. Upload the one you trust.
```

> **Step 2 is not optional.** `txt/` caches the extracted text of the *previous*
> documents. If you skip it the agent will answer using yesterday's PDFs and every
> number will be confidently wrong.

Alternatively, leave the archive where it is and point at it:

```bash
python -m agent solve ~/Downloads/private/submission_template.json \
       --archive ~/Downloads/private
```

### What will be different, and what already handles it

| Difference | Handled? |
|---|---|
| Different borrower ids (`T1`, `ACME-3`, anything) | **Yes.** The borrower list is read from `submission_template.json`; nothing is hardcoded. |
| Different account numbers | **Yes.** Resolved from the ledger's `txn_id` prefixes. |
| Different company and auditor names | **Yes.** Documents are classified by *type markers*, never by firm name. |
| More or fewer borrowers | **Yes.** Driven by the template. |
| Different clause numbers (`7.1`, `5.2`) | **Yes.** Clause list comes from the template. |
| Different thresholds | **Yes.** Parsed from each agreement. |
| **No `ground_truth.json`** | **Yes, expected.** The agent prints *"nothing to score against"* and `compare` shows changed cells instead of scores. |
| New covenant *wording* | **Partly** — see §8 |
| New transaction *description* vocabulary | **Partly** — see §8 |
| New document *types* | **Partly** — see §8 |

---

## 8. When the private data is different

This is the section that matters most. Three things can genuinely be new, and each
one **announces itself** rather than failing silently.

### A. A covenant worded in a way the library doesn't recognise

**Symptom**

```
UnmatchedCovenant: no metric in the library matches this covenant wording;
a formula must be added to metrics.LIBRARY before this cell can be computed:
  Пункт 6.1 Максимальное отношение складских запасов к выручке...
```

**Automatic fix:** run *with* the LLM. `reason.map_covenant()` reads the clause,
retrieves related passages from the agreement, and maps it onto one of 19 known
formulas.

**Manual fix (2 minutes)** — add an entry to `LIBRARY` in `pipeline/metrics.py`:

```python
("inventory_to_revenue",                          # a name
 r"Максимальное отношение складских запасов",      # distinctive wording to match
 ("inventory", "revenue"),                        # categories the formula needs
 lambda x, f, s: div(total(x, "inventory"), total(x, "revenue"))),
```

Helpers available inside the lambda: `total(x, "category")`, `ebitda(x)`, `rp(x)`
(related-party total), `div(a, b)` (zero-safe), `quoted_category(s)` (reads the
«...» category out of the clause text).

Whether it's a minimum or a maximum is detected separately, from the words
`не менее` / `не ниже` / `Минимальн` versus `не превышал` / `Максимальн` — you
don't specify it.

### B. Transactions described in unfamiliar language

**Symptom**

```
[WARN] P3: uncategorised rows ['TXN-P3-0012', ...] -- description vocabulary
       not covered by p3_categorise.RULES; they contribute to no covenant
[FAIL] P3/6.2: the covenant formula needs category 'revenue', but nothing
       matched; the emitted value is not trustworthy
```

**Automatic fix:** run with the LLM. Unplaced rows go to `reason.categorise_batch()`
with the borrower's covenant text as context.

**Manual fix** — add a pattern to `RULES` in `pipeline/p3_categorise.py`:

```python
RULES = [
    ("revenue", r"sales settlement|revenue recognition|turnover received"),
    ...
]
```

Order matters — the first match wins. Put specific patterns above general ones.

**Measured impact:** with 25% of descriptions in unseen wording, the deterministic
core alone drops from 100% to about 70%, and the health checks flag roughly 80% of
the damaged cells. This is exactly why the LLM categoriser exists.

### C. A new kind of document

The triage recognises these by header text:

| Type | Marker |
|---|---|
| `credit_agreement` | `ИСПОЛНИТЕЛЬНЫЙ ЭКЗЕМПЛЯР` or `Финансовые ковенанты` |
| `kyc_dossier` | `Знай своего клиента` |
| `financial_statements` | `Примечания к финансовой отчётности` |
| `audit_notes` | `Отчёт о выполнении согласованных процедур`, `Промежуточная ведомость` |
| *(discarded)* | `НЕДЕЙСТВУЮЩАЯ РЕДАКЦИЯ` / `НЕ ПРИМЕНЯЕТСЯ` — superseded |
| `noise` | anything else |

**Symptom of a miss:** a `FAIL` saying a borrower has no live credit agreement, or
adjustments that never bind to a row.

**Fix:** add the marker to `classify()` in `pipeline/p1_triage.py`. Match on
**document type wording**, never on a company or audit-firm name — that mistake
already cost this project once: audit reports were matched against a hardcoded
list of three firm names, so on any other dataset every audit report silently
became noise.

### D. Documents that hide authoritative data

Some numbers do not live in the obvious place. In the public archive:

- one borrower's accrued tax was only in an **internal treasury memo** that header
  triage classifies as noise — the covenant itself pointed to "treasury records"
- another's "Group capex" required deriving PPE additions from the **consolidated
  parent's** accounts: `closing NBV + depreciation − opening NBV`

**How to find these yourself:** scan every live document for money amounts that
appear in no ledger row and are not round covenant thresholds. Anything left is
either a decoy (small, a few thousand dollars) or a real hidden input.

```bash
python - <<'EOF'
import csv, re
from decimal import Decimal
from pathlib import Path
led = {abs(Decimal(r["amount"])) for r in
       csv.DictReader(open("agentic-bank-public/master_ledger_2025.csv",
                           encoding="utf-8-sig")) if r["amount"]}
AMT = re.compile(r"\$([0-9][0-9,]*\.[0-9]{2})")
for p in sorted(Path("txt").glob("*.txt")):
    t = p.read_text(encoding="utf-8", errors="replace")
    if "НЕДЕЙСТВУЮЩАЯ" in t[:3000]:
        continue
    for m in AMT.finditer(t):
        v = Decimal(m.group(1).replace(",", ""))
        if v in led or v < 100_000 or v % 10_000 == 0:
            continue
        print(f"{p.name}  ${v:,.2f}")
        print("   ", " ".join(t[max(0, m.start()-130):m.end()+30].split()), "\n")
EOF
```

---

## 9. Testing it yourself

### Test 1 — does it run and agree with the key?

```bash
python -m agent solve agentic-bank-public/submission_template.json --no-llm
```

Expect `34.500 / 36`, `35/36 statuses`, `0 FAIL`. The public archive ships
`ground_truth.json`, so this is scored automatically.

### Test 2 — simulate the private dataset

The private set has **no answer key**. Rehearse that:

```bash
cp -r agentic-bank-public /tmp/private-rehearsal
rm /tmp/private-rehearsal/ground_truth.json
rm -rf txt/ agent/work/

python -m agent solve /tmp/private-rehearsal/submission_template.json \
       --archive /tmp/private-rehearsal --no-llm
```

Expect: 36 cells written, and
`(no ground_truth.json in the archive -- nothing to score against)`.
If this works, submission day works.

### Test 3 — break something on purpose

This is the fastest way to build trust. Each experiment should produce a **loud**
failure, not a quietly wrong number. Work on a copy.

```bash
cp -r agentic-bank-public /tmp/broken && rm -rf txt/ agent/work/
```

| Experiment | Command | Expected |
|---|---|---|
| **Delete a borrower's agreement** | find its file in `index.json`, delete it from `/tmp/broken/documents/` | `FAIL … expected exactly 1 live credit agreement, found 0` |
| **Let a superseded edition through** | copy a 2024 agreement and delete the `НЕДЕЙСТВУЮЩАЯ РЕДАКЦИЯ` line from its `txt/` file | `FAIL … found 2` |
| **Change a threshold** | edit a number in the live agreement's `txt/` file | that cell's verdict changes; check `report.html` shows the new threshold |
| **Rename a description** | in the ledger CSV, change `sales settlement` to `revenue recognition` | `WARN uncategorised rows` and `FAIL … needs category 'revenue'` |
| **Corrupt an auditor note** | change the amount in a reclassification note | `FAIL … reclassification did not bind to exactly one ledger row` |
| **Remove a KYC table** | delete the ownership rows from a KYC `txt/` file | `WARN … no legible KYC ownership table; resolved by fallback` |

> Editing files in `txt/` is the quickest way to experiment — the agent reads
> `txt/` and only regenerates it from the PDFs when a file is missing.

### Test 4 — does the LLM layer help or hurt?

```bash
python -m agent solve <template> --no-llm     # try1
python -m agent solve <template>              # try2
python -m agent compare
python -m agent compare try1 try2
```

| Reading | Meaning |
|---|---|
| 0 cells differ | the rules already covered this data; either submission is fine |
| 1–3 differ | the LLM filled real gaps — check those cells in `report.html` |
| many differ | **warning sign** — the LLM may be overriding sound rules; prefer `try1` |

### Test 5 — verify a cell by hand

Pick any cell in `report.html`, read the covenant text, add up the listed rows
yourself, and check the comparison. Ten seconds per cell, 36 cells. This is worth
more than any confidence number, because it is the same check a judge would run.

---

## 10. Known limitations

Be honest about these; do not submit believing the output is perfect.

**Two cells on the public archive are not right:**

- **`P4/6.3`** — strictly, `288,417.52 / 7,004,318.47 = 0.041178`, which exceeds
  the `0.04x` cap, so the agent says BREACH. The key says COMPLIANT. Both a
  rounding rule and a tolerance band were tested and refuted by other cells. This
  one is unresolved.
- **`P9/6.1`** — the covenant counts asset transfers to *Unrestricted*
  subsidiaries. Which subsidiaries hold that status appears in **zero** of the 200
  documents. The BREACH status is right; the ratio is a guess between 0.16, 0.22
  and 0.37.

**Structural limits:**

- **Novel covenant wording** raises `UnmatchedCovenant` by design rather than
  guessing. With the LLM on it is usually mapped automatically; without it, you
  add one line to `metrics.LIBRARY`.
- **The review pass is advisory and noisy.** Measured: ~6 cells flagged per run,
  of which about 1 was a genuine problem. It never changes an answer. If time is
  short, skip it.
- **The LLM adds nothing on familiar vocabulary.** On the public archive it
  relabelled 0 rows. Its value is entirely on unseen wording.
- **The test fixture generator was lost** in a file-loss incident and has not been
  rebuilt. The manual experiments in §9 replace it.

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `pdftotext: command not found` | poppler missing | install it (§2) |
| `FAIL … no live credit agreement` | triage lost a document | check `index.json`; add the header marker to `p1_triage.classify()` |
| `FAIL … found 2 live agreements` | a superseded edition slipped through | check its `НЕДЕЙСТВУЮЩАЯ РЕДАКЦИЯ` marker |
| `UnmatchedCovenant` | new covenant wording | run with the LLM, or add to `metrics.LIBRARY` (§8A) |
| `FAIL … needs category X, nothing matched` | new description vocabulary | add to `RULES` (§8B) |
| `WARN … decoy filter retained N rows` | different ledger shape | usually harmless; spot-check a few rows |
| Numbers look like yesterday's data | stale `txt/` cache | `rm -rf txt/ agent/work/` |
| `403 error code: 1010` | Cloudflare blocking the HTTP client | already fixed — the client sends a proper `User-Agent` |
| Preflight fails but the key is valid | reasoning models need output budget | already fixed — `max_tokens=512` on preflight |
| Many `failed` in `run_meta.json` | free-tier rate limits | rerun; the cache makes it nearly free |
| Everything is broken | — | `python -m agent solve <template> --no-llm` always works without a network |

### Recovering

Every LLM call is cached on disk by content hash, so re-running after a crash
costs almost nothing. To start completely clean:

```bash
rm -rf txt/ agent/work/          # caches only; never touches the dataset
```

The dataset itself is never modified, with one exception: passing a `--template`
from outside the archive stages a copy into it.

---

## Appendix — file map

| File | Role |
|---|---|
| `agent/__main__.py` | the `python -m agent …` CLI |
| `agent/solve.py` | template in → verdicts out |
| `agent/run.py` | orchestrates all stages |
| `agent/config.py` | reads `.env`; all provider settings |
| `agent/llm.py` | OpenAI-compatible client: cache, retry, budget |
| `agent/index_build.py` | one-time pre-index → `rules_index.json`, `chunks.json` |
| `agent/retrieve.py` | RAG, scoped per borrower; BM25 fallback |
| `agent/reason.py` | the four LLM jobs |
| `agent/report.py` | renders `report.html` |
| `agent/compare.py` | score / diff attempts |
| `pipeline/p0_extract.py` | PDFs → text |
| `pipeline/p1_triage.py` | classify documents, resolve borrowers |
| `pipeline/p2_facts.py` | parse covenants, KYC, auditor notes |
| `pipeline/p3_categorise.py` | categorise rows, apply adjustments |
| `pipeline/p4_compute.py` | compute, evidence, emit, validate |
| `pipeline/metrics.py` | **the covenant formula library** |
| `pipeline/checks.py` | health checks |
| `pipeline/score.py` | scorer (validation only — never an input) |

### Environment variables

| Variable | Purpose |
|---|---|
| `HALYK_ARCHIVE` | dataset directory (set automatically by `--archive`) |
| `HALYK_ROOT` | project root |
| `AGENT_WORK` | scratch directory |
| `LLM_*` | provider settings — see §2 |

---

*Every number this agent emits is traceable to a named file or a ledger row. If
you cannot see where a number came from in `report.html`, treat that as a bug.*
