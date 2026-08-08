"""ONE-TIME pre-index. After this runs, nothing downstream reads a PDF again.

Produces two artefacts in agent/work/:

  rules_index.json   borrower -> account -> clause -> {rule text, rule_type,
                     threshold, direction, keywords, source file}
                     plus the borrower's KYC perimeter and auditor adjustments.

  chunks.json        every live document split into retrievable passages, each
                     tagged with account_id and doc_type, for RAG.

The heavy lifting (PDF extraction, triage, covenant/KYC/notes parsing) is done by
the proven deterministic pipeline; this module reshapes its output into the
lookup structure the agent actually queries.
"""
import json
import re
import sys
from pathlib import Path

from config import ROOT, WORK

sys.path.insert(0, str(ROOT / "pipeline"))


# rule_type is the retrieval key: a transaction category maps to the rule types
# whose formulas consume it, so a transaction never scans unrelated covenants.
RULE_TYPE = {
    "capital_intensity": "CAPEX",
    "category_expense_cap": "CAPEX",
    "category_revenue_floor": "REVENUE",
    "q4_revenue_floor": "REVENUE",
    "revenue_less_largest_overhead": "REVENUE",
    "interest_coverage": "INTEREST",
    "related_party_cap": "RELATED_PARTY",
    "related_party_ratio_of_revenue": "RELATED_PARTY",
    "related_party_share_of_opex": "RELATED_PARTY",
    "individual_overhead_ceiling": "OVERHEAD",
    "revenue_cover_of_payroll_and_utilities": "OVERHEAD",
    "total_personnel_obligations": "PERSONNEL",
    "tax_and_utility_burden": "TAX_UTILITY",
    "insurance_cover_of_premises_costs": "INSURANCE",
    "sources_cover_applications": "LIQUIDITY",
    "group_capex_to_ebitda": "GROUP_CAPEX",
    "unrestricted_subsidiary_transfers": "ASSET_TRANSFER",
    "springing_leverage": "LEVERAGE",
    "adjusted_ebitda_margin": "MARGIN",
}

KEYWORDS = {
    "CAPEX": ["capex", "capital expenditure", "equipment", "purchase", "капитальные затраты"],
    "REVENUE": ["revenue", "sales", "settlement", "turnover", "выручка"],
    "INTEREST": ["interest", "coupon", "facility", "процентные расходы"],
    "RELATED_PARTY": ["related party", "affiliate", "retainer", "связанные стороны"],
    "OVERHEAD": ["payroll", "labour", "utilities", "overhead", "накладные расходы"],
    "PERSONNEL": ["payroll", "severance", "headcount", "персонал"],
    "TAX_UTILITY": ["tax", "utilities", "levy", "налоги", "коммунальные"],
    "INSURANCE": ["insurance", "premium", "cover", "страховые премии"],
    "LIQUIDITY": ["financing", "drawdown", "sources", "applications", "финансирование"],
    "GROUP_CAPEX": ["group", "consolidated", "parent", "группа"],
    "ASSET_TRANSFER": ["transfer", "subsidiary", "unrestricted", "дочерн"],
    "LEVERAGE": ["leverage", "drawdown", "springing", "финансирование"],
    "MARGIN": ["ebitda", "margin", "add-back", "рентабельность"],
}


def run_deterministic_phases():
    """Extract -> triage -> parse facts. Idempotent and cached."""
    import p0_extract
    import p1_triage
    import p2_facts
    p0_extract.main()
    p1_triage.main()
    p2_facts.main()


def split_passages(text, size=900, overlap=150):
    """Paragraph-aware chunking: covenant clauses and notes stay intact."""
    parts, buf = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = re.sub(r"[ \t]+", " ", para).strip()
        if not para:
            continue
        if len(buf) + len(para) + 1 <= size:
            buf = f"{buf}\n{para}".strip()
        else:
            if buf:
                parts.append(buf)
            buf = (buf[-overlap:] + "\n" + para).strip() if buf else para
            while len(buf) > size:
                parts.append(buf[:size])
                buf = buf[size - overlap:]
    if buf:
        parts.append(buf)
    return parts


def build():
    WORK.mkdir(parents=True, exist_ok=True)
    run_deterministic_phases()

    import metrics
    index = json.loads((ROOT / "index.json").read_text(encoding="utf-8"))
    facts = json.loads((ROOT / "facts.json").read_text(encoding="utf-8"))

    borrowers, unresolved = {}, []
    for sid, acc in index["scenario_to_account"].items():
        src = facts["sources"][sid]
        clauses = {}
        for cl, spec in facts["covenants"][sid].items():
            try:
                shape, requires, _fn = metrics.resolve(spec)
            except metrics.UnmatchedCovenant:
                shape, requires = None, []
                unresolved.append(f"{sid}/{cl}")
            rt = RULE_TYPE.get(shape, "UNKNOWN")
            clauses[cl] = {
                "rule": re.sub(r"\s+", " ", spec["text"]).strip(),
                "rule_type": rt,
                "metric_shape": shape,
                "requires": list(requires),
                "threshold": spec["threshold"],
                "direction": spec["direction"],
                "unit": spec["unit"],
                "trigger_threshold": spec.get("trigger_threshold"),
                "period": [spec["period_start"], spec["period_end"]],
                "keywords": KEYWORDS.get(rt, []),
                "source": f"{src['agreement']}.pdf",
            }
        kyc = facts["kyc"].get(sid, {})
        borrowers[sid] = {
            "account_id": acc,
            "clauses": clauses,
            "related_party_threshold_pct": kyc.get("threshold_pct"),
            "ownership": kyc.get("holdings", {}),
            "indirect_holdings": kyc.get("indirect", []),
            "adjustments": facts["adjustments"][sid],
            "documents": {k: v for k, v in src.items() if v},
        }

    # reverse index: rule_type -> the (borrower, clause) pairs that use it
    by_type = {}
    for sid, b in borrowers.items():
        for cl, c in b["clauses"].items():
            by_type.setdefault(c["rule_type"], []).append(f"{sid}/{cl}")

    rules_index = {
        "borrowers": borrowers,
        "by_rule_type": by_type,
        "account_to_scenario": {v: k for k, v in index["scenario_to_account"].items()},
        "unresolved_clauses": unresolved,
        "group": facts.get("group", {}),
    }
    (WORK / "rules_index.json").write_text(
        json.dumps(rules_index, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- retrievable passages from the LIVE documents only ------------------
    acc_sid = rules_index["account_to_scenario"]
    chunks = []
    for d in index["kept"]:
        if d["is_draft"]:
            continue                      # interim schedules are never authoritative
        text = (ROOT / "txt" / d["file"]).read_text(encoding="utf-8", errors="replace")
        for i, p in enumerate(split_passages(text)):
            chunks.append({"id": f"{d['file'][:-4]}#{i}", "file": d["file"],
                           "doc_type": d["doc_type"], "account_id": d["account_id"],
                           "scenario_id": acc_sid.get(d["account_id"]), "text": p})
    (WORK / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False),
                                      encoding="utf-8")

    print(f"rules_index.json : {len(borrowers)} borrowers, "
          f"{sum(len(b['clauses']) for b in borrowers.values())} clauses, "
          f"{len(by_type)} rule types")
    print(f"chunks.json      : {len(chunks)} passages from "
          f"{len({c['file'] for c in chunks})} live documents")
    if unresolved:
        print(f"!! {len(unresolved)} clause(s) matched no metric shape: {unresolved}")
        print("   the LLM resolver will be asked to map these")
    return rules_index, chunks


if __name__ == "__main__":
    build()
