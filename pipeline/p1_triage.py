"""Phase 1-2: deterministic document triage + entity resolution.

Reads txt/*.txt (pdftotext -layout output of documents/*.pdf), classifies each
document by header markers only (no LLM judgement), hard-filters superseded
editions, and resolves account_id -> scenario_id from the ledger.

Output: index.json
"""
import csv
import json
import os
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(os.environ.get("HALYK_ROOT",
                           Path(__file__).resolve().parent.parent))
ARCHIVE = Path(os.environ.get("HALYK_ARCHIVE", ROOT / "agentic-bank-public"))
TXT = ROOT / "txt"
DATA = ARCHIVE

# borrowers to answer for come from the template, not a literal list
TARGETS = list(json.loads(
    (DATA / "submission_template.json").read_text(encoding="utf-8"))["answers"])

# Account ids are not always ACC-####: one borrower in the hidden archive uses
# TELE-4471. Match any short uppercase prefix followed by digits.
# No \b anchors: this runs on whitespace-collapsed text, where an id sits flush
# against surrounding letters ("...REFERENCEACC-7604CREDIT...") and a word
# boundary never matches. Also not restricted to ACC-: one borrower uses TELE-.
ACC_RE = re.compile(r"[A-Z]{2,6}-\d{3,6}")

# Transaction ids are not always TXN-<sid>-<seq> either: one borrower uses
# TXN-KC-CAP-29, with a category tag in the middle. Rather than guess a shape,
# resolve the borrower by longest-prefix match against the template's own ids.
_TARGETS_BY_LEN = sorted(TARGETS, key=len, reverse=True)


def scenario_of(txn_id):
    """Which target borrower does this transaction belong to? None if noise."""
    for t in _TARGETS_BY_LEN:
        if txn_id.startswith(f"TXN-{t}-"):
            return t
    return None


def load_ledger():
    rows = []
    with open(DATA / "master_ledger_2025.csv", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            r["scenario_id"] = scenario_of(r["txn_id"])
            rows.append(r)
    return rows


def scenario_accounts(rows):
    """scenario_id -> account_id, asserting the mapping is 1:1."""
    m = defaultdict(set)
    for r in rows:
        if r["scenario_id"] in TARGETS:
            m[r["scenario_id"]].add(r["account_id"])
    out = {}
    for sid, accs in m.items():
        assert len(accs) == 1, f"{sid} spans multiple accounts: {accs}"
        out[sid] = accs.pop()
    assert set(out) == set(TARGETS), f"missing scenarios: {set(TARGETS) - set(out)}"
    assert len(set(out.values())) == len(out), "account_id collision across scenarios"
    return out


def despace(text):
    """pdftotext renders letter-spaced headings as 'АУД ИТО Р С КО Е ... ACC- 7804'.
    Collapse every run of whitespace so ACC ids and markers match reliably."""
    return re.sub(r"\s+", "", text)


def classify(text):
    """Header-marker classification. Returns (doc_type, flags)."""
    # Markers come in Russian AND English: the hidden archive contains at least one
    # borrower whose entire document set is in English. Every marker below is a
    # DOCUMENT-TYPE phrase, never a company or audit-firm name.
    head = despace(text[:3000])
    whole = despace(text)
    flags = {
        "is_superseded": bool(re.search(
            r"НЕДЕЙСТВУЮЩАЯРЕДАКЦИЯ|НЕПРИМЕНЯЕТСЯ"
            r"|SUPERSEDED—?PRIOR-?YEARAGREEMENT|NOTOPERATIVE", head, re.I)),
        "is_draft": bool(re.search(
            r"ПРОЕКТ—ПРОМЕЖУТОЧН"
            r"|DRAFT—?INTERIMFIELDWORKSCHEDULE"
            r"|NOTTHEAUDITOR'?SCONCLUDEDPOSITION", head, re.I)),
    }
    if re.search(r"ИСПОЛНИТЕЛЬНЫЙЭКЗЕМПЛЯР|EXECUTIONCOPY", head, re.I) \
            or re.search(r"Финансовыековенанты|FinancialCovenants", whole, re.I):
        dt = "credit_agreement"
    elif re.search(r"Знайсвоегоклиента|KnowYourCustomer|KYC", head, re.I):
        dt = "kyc_dossier"
    elif re.search(r"Примечаниякфинансовойотчётности"
                   r"|NotestotheFinancialStatements", head, re.I):
        dt = "financial_statements"
    elif re.search(r"Отчёто?выполнениисогласованныхпроцедур"
                   r"|ОТЧЁТОВЫПОЛНЕНИИСОГЛАСОВАННЫХПРОЦЕДУР"
                   r"|Промежуточнаяведомость|ПРОМЕЖУТОЧНАЯВЕДОМОСТЬ"
                   r"|ReportonAgreed-?UponReviewProcedures"
                   r"|INTERIMFIELDWORKSCHEDULE", head, re.I):
        dt = "audit_notes"
    else:
        dt = "noise"
    return dt, flags


def entity_and_account(text, dt):
    """account_id is the only reliable borrower key: entity names are reused
    across decoy documents and do not map 1:1 to accounts."""
    flat = despace(text)
    acc = None
    # Prefer the account id printed in the document's own identification block.
    ID = r"([A-Z]{2,6}-\d{3,6})"
    for pat in (
        r"АУДИТОРСКОЕДЕЛО№" + ID,
        r"Счёт" + ID,
        r"Расчётныйсчёт" + ID,
        r"ЗАЁМ№" + ID,
        # English equivalents
        r"LOANREFERENCE" + ID,
        r"AUDITFILEREF" + ID,
        r"Account" + ID,
    ):
        m = re.search(pat, flat)
        if m:
            acc = m.group(1)
            break
    if acc is None:
        # Greedy matching can swallow preceding letters ("REFERENCEACC-7604" ->
        # "NCEACC-7604"), so score candidates by how often each id appears and
        # prefer the shortest prefix among equally frequent ones.
        hits = ACC_RE.findall(flat)
        if hits:
            acc = max(set(hits), key=lambda h: (hits.count(h), -len(h)))
    name = None
    m = re.search(r"^\s*([A-Z][A-Za-z0-9&\-\. ]+ (?:JSC|LLP|LLC|Ltd))\b", text, re.M)
    if m:
        name = m.group(1).strip()
    return name, acc


def main():
    rows = load_ledger()
    sid_acc = scenario_accounts(rows)
    acc_sid = {v: k for k, v in sid_acc.items()}

    docs = []
    for p in sorted(TXT.glob("*.txt")):
        text = p.read_text(encoding="utf-8", errors="replace")
        dt, flags = classify(text)
        name, acc = entity_and_account(text, dt)
        docs.append({
            "file": p.name,
            "doc_type": dt,
            "entity_name": name,
            "account_id": acc,
            "scenario_id": acc_sid.get(acc),
            "chars": len(text),
            **flags,
        })

    # ---- hard filters (code, not model judgement) --------------------------
    keep = [d for d in docs if d["doc_type"] != "noise" and not d["is_superseded"]]

    # Drafts are retained in the index (flagged) but NEVER treated as authoritative.
    # Their own status section states: "Настоящая ведомость полностью заменяется
    # отчётом о выполнении согласованных процедур... Следует руководствоваться
    # исключительно окончательным отчётом" and that an issue not carried into the
    # final report reverts to the original classification. So an interim schedule
    # with no corresponding final carries no adjustment at all -- it is not a
    # fallback source. p2_facts.py excludes every is_draft document from `sources`.

    # ---- assertions --------------------------------------------------------
    per_acc = defaultdict(list)
    for d in keep:
        if d["doc_type"] == "credit_agreement" and d["scenario_id"]:
            per_acc[d["account_id"]].append(d["file"])
    problems = []
    for sid, acc in sid_acc.items():
        n = len(per_acc.get(acc, []))
        if n != 1:
            problems.append(f"{sid}/{acc}: {n} live credit agreements {per_acc.get(acc)}")

    index = {
        "scenario_to_account": sid_acc,
        "documents": docs,
        "kept": keep,
        "problems": problems,
        "counts": {
            "total": len(docs),
            "kept": len(keep),
            "by_type": {t: sum(1 for d in keep if d["doc_type"] == t)
                        for t in {d["doc_type"] for d in keep}},
            "superseded_dropped": sum(1 for d in docs if d["is_superseded"]),
        },
    }
    (ROOT / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(json.dumps(index["counts"], ensure_ascii=False, indent=2))
    print("scenario -> account:", json.dumps(sid_acc, indent=None))
    if problems:
        print("\n!! PROBLEMS")
        for p in problems:
            print("  ", p)


if __name__ == "__main__":
    main()
