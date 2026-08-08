"""The agent's judgement layer.

The LLM is used for exactly three jobs, all of them classification or extraction:

  1. categorise a transaction the rule table could not place
  2. map a covenant whose wording matches no metric shape onto a known formula
  3. review a finished verdict against the retrieved clause text

It never performs arithmetic and never chooses a status directly. Every number in
the submission comes from Decimal code in pipeline/, so a hallucinated digit
cannot reach the answer -- the worst a bad LLM call can do is mislabel one input,
which the health checks then flag.

Every function degrades to None when the API is unavailable, and every caller
has a deterministic fallback.
"""
import json
import re

import llm
import retrieve

CATEGORIES = ["revenue", "financing_inflow", "capex", "capex_transfer", "opex",
              "lease", "labour", "utilities", "taxes", "interest", "insurance",
              "consulting", "other"]

_CAT_DOC = """
revenue          operating income from the borrower's core business
financing_inflow loan or facility drawdowns received
capex            purchases of plant, property or equipment
capex_transfer   transfers of capital assets to a subsidiary
opex             recurring operating and maintenance costs of running the asset
lease            rent or lease payments for land, premises or equipment
labour           payroll, wages, staff costs
utilities        electricity, water, gas, heating supply
taxes            taxes and duties paid or assessed
interest         interest and financing charges on borrowings
insurance        insurance premiums
consulting       advisory, management or retainer fees
other            anything that is not a covenant-relevant business flow
""".strip()


# --------------------------------------------------------------------- 1. categories
def categorise_batch(rows, *, covenant_context=""):
    """rows: [{txn_id, description, counterparty}]. Returns {txn_id: category}.

    Batched, temperature 0, strict JSON. The covenant definitions are included so
    the model classifies *for this contract's purpose* rather than against a
    generic chart of accounts.
    """
    if not rows:
        return {}
    listing = "\n".join(
        f'{r["txn_id"]} | {r["counterparty"]} | {r["description"]}' for r in rows)
    system = (
        "You are a credit-risk analyst categorising ledger transactions for loan "
        "covenant testing. Classify each row into exactly one category.\n\n"
        f"Categories:\n{_CAT_DOC}\n\n"
        "The description is the strongest signal; the counterparty is supporting "
        "context only. Generic administrative noise (marketing, telecom, office "
        "supplies, refunds, rebates) is 'other'. If genuinely unsure, answer "
        "'other' rather than guessing -- a wrong category corrupts a covenant "
        "total, an 'other' merely omits one row.")
    user = ""
    if covenant_context:
        user += f"The covenants being tested for this borrower:\n{covenant_context}\n\n"
    user += f"Classify these transactions:\n{listing}"
    out = llm.chat_json(
        system, user,
        schema_hint='{"TXN-XX-0001": {"category": "opex", "confidence": 0.0-1.0}}',
        max_tokens=min(4000, 120 + 60 * len(rows)))
    if not isinstance(out, dict):
        return {}
    res = {}
    for tid, v in out.items():
        cat = v.get("category") if isinstance(v, dict) else v
        conf = float(v.get("confidence", 1)) if isinstance(v, dict) else 1.0
        if cat in CATEGORIES and conf >= 0.55:
            res[tid] = cat
    return res


# --------------------------------------------------------------------- 2. covenants
_SHAPE_DOC = """
capital_intensity                       capex / (opex + lease)
interest_coverage                       (revenue - opex) / interest
category_revenue_floor                  total revenue
category_expense_cap                    total of one named expense category
related_party_cap                       total payments to related parties
related_party_ratio_of_revenue          related-party payments / revenue
related_party_share_of_opex             related-party payments / opex
individual_overhead_ceiling             max(labour, utilities)
q4_revenue_floor                        revenue recognised in Q4 only
tax_and_utility_burden                  (taxes + utilities) / (revenue - opex)
total_personnel_obligations             labour + disclosed severance liability
insurance_cover_of_premises_costs       insurance / (lease + utilities)
sources_cover_applications              (revenue + financing) / (opex + capex)
revenue_cover_of_payroll_and_utilities  revenue / (labour + utilities)
revenue_less_largest_overhead           revenue - max(labour, taxes)
group_capex_to_ebitda                   group capex / (revenue - opex)
unrestricted_subsidiary_transfers       transfers to subsidiaries / total capex
springing_leverage                      financing / (revenue - opex), conditional
adjusted_ebitda_margin                  (revenue - opex + add-backs) / revenue
""".strip()


def map_covenant(clause_text, *, scenario_id=None):
    """Map an unrecognised covenant onto a known metric shape.

    Only called when metrics.LIBRARY matched nothing -- i.e. the graded archive
    used wording we have not seen. Returns {shape, direction, confidence,
    reasoning} or None.
    """
    ctx = ""
    if scenario_id:
        ctx, _ = retrieve.context_for(
            scenario_id, clause_text[:400], k=3, doc_types=["credit_agreement"])
    system = (
        "You map Russian loan-covenant clauses onto a fixed library of metric "
        "formulas. Choose the ONE formula whose definition matches what the clause "
        "constrains.\n\n"
        f"Available shapes:\n{_SHAPE_DOC}\n\n"
        "Also state whether the covenant sets a MINIMUM the metric must reach "
        "('не менее', 'не ниже', 'минимальн') or a MAXIMUM it must not exceed "
        "('не превышал', 'максимальн'). If no shape genuinely fits, return "
        'shape=null -- do not force a match.')
    user = (f"Clause text:\n{clause_text}\n\n"
            + (f"Related passages from the agreement:\n{ctx}\n" if ctx else ""))
    out = llm.chat_json(
        system, user,
        schema_hint='{"shape": "capital_intensity" or null, '
                    '"direction": "min"|"max", "confidence": 0.0-1.0, '
                    '"reasoning": "one sentence"}')
    if not isinstance(out, dict) or not out.get("shape"):
        return None
    if out.get("direction") not in ("min", "max"):
        return None
    return out


# --------------------------------------------------------------------- 3. review
def review_cell(sid, clause, cell, *, trail):
    """Second opinion on a finished verdict.

    The reviewer sees the clause text retrieved from the agreement, the computed
    metric and the comparison -- and is asked only whether the READING is right,
    never to recompute. Returns {agree: bool, concern: str, confidence: float}.
    """
    # The KYC dossier MUST be retrievable here. A related-party clause says who
    # counts is "per the client identification dossier" -- and it is the dossier,
    # not the agreement, that states the percentage threshold. Without it in
    # context a reviewer reliably (and wrongly) objects that a percentage cut-off
    # was invented.
    ctx, _ = retrieve.context_for(
        sid, cell["covenant_text"][:400], k=5,
        doc_types=["credit_agreement", "financial_statements", "audit_notes",
                   "kyc_dossier"])
    system = (
        "You are reviewing another analyst's covenant assessment. You are NOT "
        "recomputing anything -- the arithmetic is already verified. Judge only "
        "whether the metric definition and the comparison direction match what "
        "the clause actually says.\n\n"
        "Read the supporting passages before objecting. Four things are already "
        "settled and are NOT grounds for disagreement:\n"
        "1. When a clause defers a definition to another document (the KYC "
        "dossier, the audited statements, the auditor's notes, treasury records), "
        "the threshold or list in THAT document is the correct source. Using it is "
        "not an invention.\n"
        "2. 'независимо от назначения платежа' means regardless of how the payment "
        "was DESCRIBED in the books. It does not remove an ownership threshold.\n"
        "3. The transaction ledger IS the borrower's accounting record. Phrases "
        "like 'amounts attributed to this line in the audited financial "
        "statements' mean the ledger as adjusted by the auditor's notes -- there "
        "is no separate line-item statement in this corpus. Sourcing figures from "
        "the ledger plus disclosed adjustments is correct.\n"
        "4. The adjustment trail below covers the WHOLE borrower, so it mentions "
        "rows belonging to that borrower's other two covenants. Only object about "
        "a transaction if it actually feeds the metric named above.\n\n"
        "Answer agree=false ONLY if you can name the specific misreading and point "
        "at the passage that contradicts it. Vague unease is agree=true with a low "
        "confidence.")
    user = (
        f"Clause {clause} of borrower {sid}.\n\n"
        f"Clause text:\n{cell['covenant_text']}\n\n"
        f"Analyst's reading: {cell['metric']}\n"
        f"Computed value: {cell['raw']}\n"
        f"Threshold: {cell['threshold']} ({cell['direction']})\n"
        f"Test applied: {cell['comparison']}\n"
        f"Verdict: {cell['status']}\n\n"
        f"How the inputs were assembled:\n" + "\n".join(f"- {t}" for t in trail[:12])
        + (f"\n\nSupporting passages:\n{ctx}" if ctx else ""))
    out = llm.chat_json(
        system, user,
        schema_hint='{"agree": true, "concern": "", "confidence": 0.0-1.0}',
        max_tokens=4000)   # reasoning models spend tokens before emitting
    if not isinstance(out, dict) or "agree" not in out:
        return None
    return {"agree": bool(out["agree"]),
            "concern": str(out.get("concern", ""))[:400],
            "confidence": float(out.get("confidence", 0.5))}


def cross_check(sid, clause, cell, trail):
    """Ask the secondary model the same review question. Disagreement between two
    models is a stronger signal than either one alone."""
    from config import LLMConfig
    if not LLMConfig.model_b:
        return None
    primary = LLMConfig.model
    try:
        LLMConfig.model = LLMConfig.model_b
        return review_cell(sid, clause, cell, trail=trail)
    finally:
        LLMConfig.model = primary
