"""Health checks: make every heuristic fail LOUDLY on an unseen archive.

Three rules in this pipeline are tuned to the shape of the corpus rather than to
a document that states them:

  * the decoy filter (counterparty prefixes harvested from the fake 9xxx ids)
  * the description -> category taxonomy in p3_categorise.RULES
  * the retainer-counterparty fallback when a KYC ownership table is unreadable

If a regenerated archive changes that shape, the danger is not that the pipeline
crashes -- it is that a category silently matches nothing, a total comes out as
zero, and a plausible-looking wrong number gets emitted. Every check below turns
one of those silent failures into a visible one.

FAIL = the affected cell is probably wrong. WARN = degraded but defensible.
"""
from decimal import Decimal

FAIL, WARN = "FAIL", "WARN"


class Issue:
    def __init__(self, level, where, msg):
        self.level, self.where, self.msg = level, where, msg

    def __repr__(self):
        return f"[{self.level}] {self.where}: {self.msg}"


# --------------------------------------------------------------------------- triage
def check_triage(index, targets):
    out = []
    kept = index["kept"]
    for sid in targets:
        acc = index["scenario_to_account"].get(sid)
        if acc is None:
            out.append(Issue(FAIL, sid, "no account_id resolved from the ledger"))
            continue
        live = [d for d in kept if d["scenario_id"] == sid
                and d["doc_type"] == "credit_agreement" and not d["is_draft"]]
        if len(live) != 1:
            out.append(Issue(FAIL, sid, f"expected exactly 1 live credit agreement for "
                                        f"{acc}, found {len(live)}: "
                                        f"{[d['file'] for d in live]}"))
        if not any(d["scenario_id"] == sid and d["doc_type"] == "financial_statements"
                   for d in kept):
            out.append(Issue(WARN, sid, "no auditor financial-statement notes; any "
                                        "reclassification / cut-off / FX / off-ledger "
                                        "adjustment for this borrower would be missed"))
    unassigned = [d["file"] for d in kept if not d["scenario_id"]]
    if unassigned:
        out.append(Issue(FAIL, "triage", f"kept documents with no borrower: {unassigned}"))
    return out


# --------------------------------------------------------------------------- covenants
def check_covenants(facts, targets):
    out = []
    for sid in targets:
        cov = facts["covenants"].get(sid, {})
        if set(cov) != {"6.1", "6.2", "6.3"}:
            out.append(Issue(FAIL, sid, f"parsed clauses {sorted(cov)} != 6.1/6.2/6.3"))
        for clause, spec in cov.items():
            try:
                thr = Decimal(spec["threshold"])
            except Exception:
                out.append(Issue(FAIL, f"{sid}/{clause}", "threshold did not parse"))
                continue
            if thr <= 0:
                out.append(Issue(FAIL, f"{sid}/{clause}", f"non-positive threshold {thr}"))
            if spec["direction"] not in ("min", "max"):
                out.append(Issue(FAIL, f"{sid}/{clause}",
                                 f"direction {spec['direction']!r} not min/max"))
    return out


# --------------------------------------------------------------------------- categorisation
# a borrower carries one covenant-relevant row per referenced category; the
# observed band on the reference archive is 4-9. Well outside it means the decoy
# filter has under- or over-reached on this archive.
LO, HI = 3, 14


def check_categorisation(txns_by_sid, targets):
    out = []
    for sid in targets:
        rows = txns_by_sid.get(sid, [])
        if not (LO <= len(rows) <= HI):
            out.append(Issue(WARN, sid, f"decoy filter retained {len(rows)} rows "
                                        f"(expected {LO}-{HI}); the counterparty pool "
                                        f"may not describe this archive"))
        other = [t["txn_id"] for t in rows if t["category"] == "other"]
        if other:
            out.append(Issue(WARN, sid, f"uncategorised rows {other} -- description "
                                        f"vocabulary not covered by p3_categorise.RULES; "
                                        f"they contribute to no covenant"))
        missing = [t["txn_id"] for t in rows if t["amount"] is None]
        if missing:
            out.append(Issue(FAIL, sid, f"rows with no amount and no disclosure to "
                                        f"restore it: {missing}"))
    return out


# --------------------------------------------------------------------------- adjustments
def check_adjustments(facts, txns_by_sid, targets):
    """Every note the auditor wrote must bind to exactly one ledger row."""
    out = []
    for sid in targets:
        adj = facts["adjustments"].get(sid, {})
        rows = txns_by_sid.get(sid, [])
        ids = {t["txn_id"] for t in rows}
        for rc in adj.get("reclass", []):
            if not any(t.get("reclassified") and
                       t["amount"] is not None and
                       abs(t["amount"]) == Decimal(rc["amount"]) for t in rows):
                out.append(Issue(FAIL, sid, f"auditor reclassification of ${rc['amount']} "
                                            f"to {rc['counterparty']!r} did not bind to "
                                            f"exactly one ledger row"))
        for key, label in (("cutoff", "cut-off exclusion"),
                           ("offledger", "off-ledger amount"),
                           ("rejected", "rejected reclassification")):
            for item in adj.get(key, []):
                if item["txn_id"] not in ids:
                    out.append(Issue(FAIL, sid, f"{label} names {item['txn_id']}, which is "
                                                f"not among this borrower's retained rows"))
        eur = [t["txn_id"] for t in rows
               if t["currency"] == "EUR" and "amount_usd" not in t
               and t["amount"] is not None]
        if eur:
            out.append(Issue(WARN, sid, f"EUR rows with no disclosed settlement pair, "
                                        f"counted at face value: {eur}"))
        kyc = facts["kyc"].get(sid, {})
        if kyc.get("threshold_pct") is None:
            used = [t["txn_id"] for t in rows if t["related_party"]]
            out.append(Issue(WARN, sid, f"no legible KYC ownership table; related parties "
                                        f"resolved by the retainer fallback -> {used}"))
    return out


# --------------------------------------------------------------------------- metric inputs
def check_metric_inputs(sid, clause, requires, txns, facts, value):
    """A formula that references a category which matched no row produces a
    confidently wrong number. Catch it before it reaches submission.json."""
    out = []
    for req in requires:
        if req == "@related":
            ok = any(t["related_party"] and not t["excluded"] for t in txns)
            what = "related-party rows"
        elif req == "@group":
            ok = bool(facts.get("group", {}).get("parent", {}).get("additions"))
            what = "consolidated parent capex"
        elif req == "@disclosed":
            ok = bool(facts["_adj"]["disclosed_liabilities"])
            what = "disclosed off-balance liability"
        else:
            ok = any(t["category"] == req and not t["excluded"]
                     and t["amount"] is not None for t in txns)
            what = f"category {req!r}"
        if not ok:
            out.append(Issue(FAIL, f"{sid}/{clause}",
                             f"the covenant formula needs {what}, but nothing matched; "
                             f"the emitted value is not trustworthy"))
    if value == 0:
        out.append(Issue(FAIL, f"{sid}/{clause}", "metric evaluated to 0 -- either a "
                                                  "division by an empty denominator or "
                                                  "every input was filtered away"))
    return out


# --------------------------------------------------------------------------- report
def report(issues, strict=True):
    fails = [i for i in issues if i.level == FAIL]
    warns = [i for i in issues if i.level == WARN]
    print(f"\n--- health: {len(fails)} FAIL, {len(warns)} WARN")
    for i in fails + warns:
        print(f"  {i}")
    if not issues:
        print("  all checks clean")
    if fails and strict:
        raise SystemExit(f"\n{len(fails)} FAIL-level issue(s): submission.json was still "
                         f"written, but the flagged cells are not trustworthy. "
                         f"Investigate before submitting.")
    return fails, warns
