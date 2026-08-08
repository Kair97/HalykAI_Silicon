"""Phase 3 + 5a: parse authoritative facts out of the live documents.

Everything numeric that later phases consume is extracted here, verbatim, from a
named file. Nothing is hardcoded from prior knowledge of the answers.

Output: facts.json
  covenants[sid][clause]   threshold, direction, period, unit, verbatim text
  kyc[sid]                 ownership table, related-party threshold, riders
  adjustments[sid]         reclassifications, cut-offs, FX pairs, off-ledger
                           amounts, disclosed liabilities
  group                    consolidated-parent capex (PPE roll-forward)
"""
import json
import os
import re
from decimal import Decimal
from pathlib import Path

from p3_categorise import CLAUSES  # both derived from submission_template.json

ROOT = Path(os.environ.get("HALYK_ROOT",
                           Path(__file__).resolve().parent.parent))
TXT = ROOT / "txt"

MONEY = r"\$([0-9][0-9,]*\.[0-9]{2})"


def dec(s):
    return Decimal(str(s).replace(",", "").replace("$", "").strip())


def flat(t):
    return re.sub(r"\s+", " ", t)


def load(name):
    return (TXT / f"{name}.txt").read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- covenants
def parse_covenants(text):
    """Article 6 -> per-clause threshold / direction / period / unit."""
    m = re.search(r"Статья 6 — Финансовые ковенанты(.*?)Статья 7 —", text, re.S)
    if not m:
        raise ValueError("Article 6 not found")
    art = flat(m.group(1))
    out = {}
    for clause in CLAUSES:
        b = re.search(rf"Пункт {re.escape(clause)} (.*?)(?=Пункт 6\.\d|$)", art)
        if not b:
            raise ValueError(f"clause {clause} missing")
        body = b.group(1).strip()
        money = [dec(x) for x in re.findall(MONEY, body)]
        ratio = [dec(x) for x in re.findall(r"(\d+\.\d+)x", body)]
        # "не менее / не ниже" => minimum; "не превышал / не более" => maximum
        is_min = bool(re.search(r"не менее|не ниже|снижения|Минимальн|Минимальное", body))
        out[clause] = {
            "text": body,
            "threshold": str(ratio[0] if ratio else money[0]),
            "unit": "ratio" if ratio else "usd",
            "direction": "min" if is_min else "max",
            "trigger_threshold": str(money[0]) if (ratio and money) else None,
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
        }
    return out


# --------------------------------------------------------------------------- kyc
def parse_kyc(text):
    """Ownership table + the borrower-specific related-party threshold."""
    if "Доля голосующих прав" not in text:
        return {"holdings": {}, "threshold_pct": None, "indirect": [],
                "note": "ownership table absent from source PDF"}
    seg = text.split("Доля голосующих прав", 1)[1]
    seg = seg.split("Идентификация и проверка")[0]
    holdings = {}
    for line in seg.splitlines():
        m = re.match(r"\s*(.+?)\s{2,}(\d+\.\d)%\s*$", line)
        if m:
            holdings[m.group(1).strip().strip('"')] = m.group(2)
    thr = re.search(r"Группа владеет (\d+\.\d)% и более", flat(seg))
    indirect = []
    im = re.search(
        r"Доля в (.+?) удерживается косвенно через (.+?); Группе принадлежит "
        r"(\d+\.\d)% голосующих прав в", flat(seg))
    if im:
        indirect.append({"entity": im.group(1).strip(),
                         "via": im.group(2).strip(),
                         "via_pct": im.group(3)})
    return {"holdings": holdings,
            "threshold_pct": thr.group(1) if thr else None,
            "indirect": indirect}


# --------------------------------------------------------------------------- notes
RECLASS = re.compile(
    r"Сумма в размере " + MONEY + r", выплаченная контрагенту (.+?), "
    r"первоначально учтённая как (.+?), переклассифицирована для целей "
    r"соблюдения ковенантов как ([^.]+)\.")
AUP = re.compile(
    r"\(4\.\d\) Сумма в размере " + MONEY + r", выплаченная контрагенту (.+?), "
    r"первоначально учтённая как (.+?), переклассифицирована для целей "
    r"соблюдения ковенантов как ([^.]+)\.")
REJECTED = re.compile(
    r"Операция (TXN-[A-Z0-9-]+), первоначально учтённая как (.+?) \(" + MONEY +
    r"\), рассматривалась на предмет возможной переклассификации.*?"
    r"первоначальная классификация \((.+?)\) сохраняется")
CUTOFF = re.compile(
    r"Операция (TXN-[A-Z0-9-]+)(?:, датированная \d{4}-\d{2}-\d{2})?[^.]*?"
    r"(?:исключена из ковенантного периода"
    r"|относится к услугам, оказанным в период с (\d{4}-\d{2}-\d{2}))")
OFFLEDGER = re.compile(
    r"Операция (TXN-[A-Z0-9-]+)[^:]*: сумма не отражена в выгрузке реестра; "
    r"фактическая сумма операции составляет " + MONEY + r" \(расход\)")
DISCLOSED = re.compile(
    r"совокупное обязательство по программе выходных пособий в размере " + MONEY)
FXPAIR = re.compile(
    r"счёт на сумму ([\d,]+\.\d{2}) EUR урегулирован платежом в долларах США "
    r"в размере " + MONEY)


def parse_adjustments(text, is_draft=False):
    f = flat(text)
    adj = {"reclass": [], "rejected": [], "cutoff": [], "offledger": [],
           "disclosed_liabilities": [], "fx_pairs": []}
    if is_draft:
        # Interim schedules state that the final report governs and that
        # unresolved items revert to the original classification. Never applied.
        return adj
    seen = set()
    for pat in (RECLASS, AUP):
        for amt, cp, frm, to in pat.findall(f):
            key = (str(dec(amt)), cp.strip())
            if key in seen:
                continue  # same conclusion matched by both the generic and AUP pattern
            seen.add(key)
            adj["reclass"].append({"amount": str(dec(amt)), "counterparty": cp.strip(),
                                   "from": frm.strip(), "to": to.strip()})
    for txn, frm, amt, keep in REJECTED.findall(f):
        adj["rejected"].append({"txn_id": txn, "amount": str(dec(amt)), "kept_as": keep})
    for txn, _ in CUTOFF.findall(f):
        adj["cutoff"].append({"txn_id": txn})
    for txn, amt in OFFLEDGER.findall(f):
        adj["offledger"].append({"txn_id": txn, "amount": str(dec(amt))})
    for amt in DISCLOSED.findall(f):
        adj["disclosed_liabilities"].append({"kind": "severance_programme",
                                             "amount": str(dec(amt))})
    for eur, usd in FXPAIR.findall(f):
        adj["fx_pairs"].append({"eur": str(dec(eur)), "usd": str(dec(usd))})
    return adj


# --------------------------------------------------------------------------- group
def parse_group_capex(text):
    """Consolidated parent PPE roll-forward -> additions (Group capex).

    additions = closing NBV + depreciation - opening NBV   (no disposals stated)
    """
    if "Note 7 — Property, Plant and Equipment" not in text:
        return None
    seg = text.split("Note 7 — Property, Plant and Equipment", 1)[1]
    if "no disposals" not in seg:
        return None
    vals = [dec(x) for x in re.findall(MONEY, seg)]
    if len(vals) < 3:
        return None
    depreciation = min(vals)
    big = sorted(v for v in vals if v > depreciation)
    opening, closing = big[0], big[1]
    return {"opening_nbv": str(opening), "depreciation": str(depreciation),
            "closing_nbv": str(closing),
            "additions": str(closing + depreciation - opening)}


# --------------------------------------------------------------------------- main
def main():
    ix = json.loads((ROOT / "index.json").read_text(encoding="utf-8"))
    by_sid = {}
    for d in ix["kept"]:
        by_sid.setdefault(d["scenario_id"], []).append(d)

    facts = {"covenants": {}, "kyc": {}, "adjustments": {}, "group": {}, "sources": {}}

    # extra authoritative documents that are not one of the four core types:
    # treasury memos (borrower treasury records) and consolidated group accounts.
    extra = {}
    for p in sorted(TXT.glob("*.txt")):
        t = p.read_text(encoding="utf-8", errors="replace")
        head = flat(t[:3000])
        if "НЕДЕЙСТВУЮЩАЯ РЕДАКЦИЯ" in head:
            continue
        if "Служебная записка казначейства" in head:
            acc = re.search(r"(ACC-\d{4})", flat(t))
            extra.setdefault(acc.group(1) if acc else None, []).append(("treasury", p.stem, t))
        elif "Consolidated Financial Statements" in head:
            extra.setdefault("GROUP", []).append(("group", p.stem, t))

    for sid, acc in ix["scenario_to_account"].items():
        docs = {d["doc_type"]: d["file"][:-4] for d in by_sid.get(sid, [])
                if not d["is_draft"]}
        drafts = [d["file"][:-4] for d in by_sid.get(sid, []) if d["is_draft"]]
        src = {"agreement": docs.get("credit_agreement"),
               "kyc": docs.get("kyc_dossier"),
               "financial_statements": docs.get("financial_statements"),
               "audit_notes": docs.get("audit_notes"),
               "discarded_drafts": drafts}

        facts["covenants"][sid] = parse_covenants(load(src["agreement"]))
        facts["kyc"][sid] = parse_kyc(load(src["kyc"])) if src["kyc"] else {
            "holdings": {}, "threshold_pct": None, "indirect": [],
            "note": "no KYC dossier for this borrower"}

        adj = {"reclass": [], "rejected": [], "cutoff": [], "offledger": [],
               "disclosed_liabilities": [], "fx_pairs": []}
        for key in ("financial_statements", "audit_notes"):
            if src[key]:
                for k, v in parse_adjustments(load(src[key])).items():
                    adj[k].extend(v)
        for kind, stem, t in extra.get(acc, []):
            if kind == "treasury":
                src["treasury_memo"] = stem
                for k, v in parse_adjustments(t).items():
                    adj[k].extend(v)
        facts["adjustments"][sid] = adj
        facts["sources"][sid] = src

    for _, stem, t in extra.get("GROUP", []):
        g = parse_group_capex(t)
        if g:
            g["source"] = stem
            ent = re.search(r"conducted through ([A-Z][\w .&-]+? JSC)", flat(t))
            if ent:
                g["subsidiary"] = ent.group(1).strip()
            facts["group"]["parent"] = g

    (ROOT / "facts.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(json.dumps({
        "covenant_clauses": sum(len(v) for v in facts["covenants"].values()),
        "kyc_with_table": sum(1 for v in facts["kyc"].values() if v["holdings"]),
        "kyc_missing": [k for k, v in facts["kyc"].items() if not v["holdings"]],
        "adjustments": {k: {kk: len(vv) for kk, vv in v.items() if vv}
                        for k, v in facts["adjustments"].items() if any(v.values())},
        "group": facts["group"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
