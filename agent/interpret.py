"""LLM covenant interpreter: read a clause, emit an executable formula spec.

This is the part of the agent that genuinely reasons. Everything downstream is
arithmetic.

The model never returns a number. It returns a STRUCTURE describing what the
covenant measures:

    {"operation": "ratio",
     "numerator":   ["capex"],
     "denominator": ["opex", "lease"],
     "direction": "max",
     "threshold": "0.42",
     "period": "year"}

Python then evaluates that structure over the categorised ledger in Decimal. So
a misread clause costs one wrong formula -- which the cross-check catches -- but
a hallucinated digit can never reach the answer, because the model is never
asked for one.
"""
import json
import re

import llm

CATEGORIES = ["revenue", "financing_inflow", "debt_repayment", "capex",
              "capex_transfer", "opex", "lease", "labour", "utilities", "taxes",
              "interest", "insurance", "consulting", "marketing"]

OPERATIONS = {
    "sum": "add the numerator categories together",
    "ratio": "numerator total divided by denominator total",
    "difference": "numerator total minus denominator total",
    "max_of": "the LARGEST single category total among the numerator categories",
    "min_of": "the SMALLEST single category total among the numerator categories",
    "difference_max": "numerator total minus the LARGEST single denominator category",
}

PERIODS = {
    "year": "the whole covenant period",
    "q4": "only the fourth fiscal quarter",
    "quarter_min": "the WORST (lowest) single fiscal quarter",
    "quarter_max": "the HIGHEST single fiscal quarter",
    "quarter_share": "highest single quarter divided by the full-year total",
}

SYSTEM = f"""You are a credit-risk analyst translating a loan covenant into an
executable formula. You NEVER compute or estimate any number.

Available transaction categories:
{chr(10).join('  ' + c for c in CATEGORIES)}

Special tokens you may use in place of a category:
  @related      payments to related parties (identified from the KYC dossier)
  @group_capex  capital expenditure of the consolidated GROUP parent
  @disclosed    a liability disclosed in the notes but absent from the ledger
  @one_off      non-recurring items the auditors add back to EBITDA
  @net_debt     financing drawn in the period MINUS principal repayments

Operations:
{chr(10).join(f'  {k}: {v}' for k, v in OPERATIONS.items())}

Period handling:
{chr(10).join(f'  {k}: {v}' for k, v in PERIODS.items())}

Rules you must follow:
- EBITDA is revenue minus opex. Express it as numerator ["revenue"],
  denominator ["opex"], operation "difference" -- or nest it where needed.
- "direction" is "min" if the covenant sets a floor the metric must reach
  (не менее / не ниже / Минимальн / at least), "max" if it sets a ceiling
  (не превышал / Максимальн / shall not exceed).
- "threshold" is the number the METRIC is compared against. A conditional or
  springing covenant also states a TRIGGER; put that in "trigger" and never
  confuse the two. If the metric is a ratio the threshold is the "N.NNx" figure;
  if the metric is an amount the threshold is the "$N" figure.
- Report the metric the covenant CONSTRAINS, never the trigger value.
- If you cannot determine the formula with confidence, set "operation" to null.
"""

SCHEMA = """{"operation": "ratio", "numerator": ["capex"],
 "denominator": ["opex","lease"], "direction": "max", "threshold": "0.42",
 "trigger": null, "period": "year", "confidence": 0.0-1.0,
 "reasoning": "one short sentence"}"""


def derive(clause_text, *, clause_id=""):
    """Clause text -> formula spec, or None if the model declines/fails."""
    out = llm.chat_json(
        SYSTEM,
        f"Covenant clause {clause_id}:\n\n{clause_text}",
        schema_hint=SCHEMA, max_tokens=2500)
    if not isinstance(out, dict) or not out.get("operation"):
        return None
    if out["operation"] not in OPERATIONS:
        return None
    if out.get("direction") not in ("min", "max"):
        return None
    ok = CATEGORIES + ["@related", "@group_capex", "@disclosed", "@one_off",
                       "@net_debt"]
    for key in ("numerator", "denominator"):
        vals = out.get(key) or []
        if not isinstance(vals, list) or any(v not in ok for v in vals):
            return None
    if not out.get("numerator"):
        return None
    if out.get("period") not in PERIODS:
        out["period"] = "year"
    thr = str(out.get("threshold", "")).replace(",", "").replace("$", "").strip()
    if not re.fullmatch(r"\d+(\.\d+)?", thr or ""):
        return None
    out["threshold"] = thr
    return out


def derive_all(covenants, *, on_progress=None):
    """{sid: {clause: spec}} -> {(sid, clause): formula}. Cached per clause text."""
    got = {}
    for sid, clauses in covenants.items():
        for cl, spec in clauses.items():
            text = re.sub(r"\s+", " ", spec.get("text", ""))
            if not text:
                continue
            f = derive(text, clause_id=f"{sid} {cl}")
            if f:
                got[(sid, cl)] = f
            if on_progress:
                on_progress(sid, cl, f)
    return got
