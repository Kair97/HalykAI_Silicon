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

TXN_RE = re.compile(r"^TXN-([A-Z0-9]+)-(\d+)$")
ACC_RE = re.compile(r"ACC-\d{4}")


def load_ledger():
    rows = []
    with open(DATA / "master_ledger_2025.csv", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            m = TXN_RE.match(r["txn_id"])
            r["scenario_id"] = m.group(1) if m else None
            r["seq"] = int(m.group(2)) if m else None
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
    head = despace(text[:3000])
    flags = {
        "is_superseded": bool(re.search(r"НЕДЕЙСТВУЮЩАЯРЕДАКЦИЯ|НЕПРИМЕНЯЕТСЯ", head)),
        "is_draft": bool(re.search(r"ПРОЕКТ—ПРОМЕЖУТОЧН", head)),
    }
    if "ИСПОЛНИТЕЛЬНЫЙЭКЗЕМПЛЯР" in head or "Финансовыековенанты" in despace(text):
        dt = "credit_agreement"
    elif "Знайсвоегоклиента" in head:
        dt = "kyc_dossier"
    elif "Примечаниякфинансовойотчётности" in head:
        dt = "financial_statements"
    # Classify auditor output by DOCUMENT TYPE, never by firm name: a regenerated
    # archive uses different audit firms, and a firm-name list silently demotes
    # every audit report to noise.
    elif re.search(r"Отчёто?выполнениисогласованныхпроцедур"
                   r"|ОТЧЁТОВЫПОЛНЕНИИСОГЛАСОВАННЫХПРОЦЕДУР"
                   r"|Промежуточнаяведомость|ПРОМЕЖУТОЧНАЯВЕДОМОСТЬ", head, re.I):
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
    for pat in (
        r"АУДИТОРСКОЕДЕЛО№(ACC-\d{4})",
        r"Счёт(ACC-\d{4})",
        r"Расчётныйсчёт(ACC-\d{4})",
    ):
        m = re.search(pat, flat)
        if m:
            acc = m.group(1)
            break
    if acc is None:
        hits = ACC_RE.findall(flat)
        if hits:
            acc = max(set(hits), key=hits.count)  # most frequent mention wins
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
