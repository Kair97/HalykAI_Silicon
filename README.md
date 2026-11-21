# Covenant Compliance Agent

Team Silicon's agent for the **Halyk AI Challenge** (August 2026). It reads a bank's loan documents and transaction ledger, then decides whether each borrower met each loan covenant, with the actual value and the transaction that proves it.

**Result on the public archive: 34.5 / 36 (95.8%).** 35 of 36 statuses are correct and 33 cells are fully correct.

## The problem

| | |
|---|---|
| **Input** | About 200 hash-named PDFs (roughly 150 are decoys), a ledger of 1,473 transactions, and a submission template |
| **Output** | For each borrower × covenant: `status`, `actual` value, `evidence_txn_id` |
| **Scoring** | status 0.50 · actual 0.30 · evidence 0.20 |

## How it works

```text
Triage → Parse → Categorise → Adjust → Compute → Evidence → Emit
```

1. **Triage** separates relevant documents from decoys.
2. **Parse** extracts covenant clauses and definitions from the PDFs.
3. **Categorise** maps ledger transactions to the terms used in each agreement.
4. **Adjust** applies the adjustments the documents describe.
5. **Compute** builds the formula from the clause wording, using a library of 19 metric shapes.
6. **Evidence** finds the deciding transaction with a counterfactual check.
7. **Emit** writes the submission file and an HTML report.

## Design decisions

- **The LLM never does arithmetic.** All money calculations use Python `Decimal`.
- **Unknown clauses fail loudly.** The agent raises `UnmatchedCovenant` instead of guessing a formula.
- **Rules stay the default.** LLM-derived formulas scored 34.0 and rules scored 34.5, so the agent runs fully without an LLM.

## Tech stack

Python 3.11+ (standard library only) · `pdftotext` · optional LLM through Groq

## Quick start

```bash
python -m agent check
python -m agent solve agentic-bank-public/submission_template.json --no-llm
```

The report is written to `agent/runs/try1/report.html`. A cold run takes about 12 seconds and a warm run takes under 1 second.

## Known limitations

- **P4 / covenant 6.3:** computed 0.041178 against a 0.04 cap.
- **P9 / covenant 6.1:** depends on Unrestricted subsidiaries that are not in the documents.

## Documentation

- [Complete guide](HOWTO.md)
- [Agent internals](agent/README.md)
