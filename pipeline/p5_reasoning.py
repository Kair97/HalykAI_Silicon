"""Write reasoning/<scenario>.md: the audit trail behind every emitted cell."""
import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("HALYK_ROOT",
                           Path(__file__).resolve().parent.parent))
OUT = ROOT / "reasoning"

CAVEATS = {
    ("P4", "6.1"): "The auditor's Note 8 (EBITDA adjustments) is a **blank page** in "
                   "the source PDF. The covenant defers the materiality floor for "
                   "add-backs to that disclosed policy. With no floor disclosed, every "
                   "item the auditor would treat as non-recurring is added back. "
                   "**This value is uncertain.**",
    ("P4", "6.3"): "Computed strictly: 0.041178 exceeds the 0.04x cap, hence BREACH. "
                   "The metric rounds to 0.04 at the emitted precision, so a reader "
                   "comparing the *rounded* figure would call this COMPLIANT. We report "
                   "the literal comparison on the unrounded value.",
    ("P9", "6.1"): "No document states which subsidiaries hold Unrestricted status; "
                   "neither transfer recipient appears in the KYC dossier. Both are "
                   "treated as outside the Lender's security package. **The numerator "
                   "is uncertain**; the BREACH status holds under every reading except "
                   "'neither is Unrestricted'.",
    ("P2", "6.3"): "The ownership table is **missing from the source PDF** of this KYC "
                   "dossier. Related-party status is resolved by the retainer-"
                   "counterparty fallback, which holds in 10/10 legible borrowers.",
    ("P6", "6.1"): "This borrower has **no KYC dossier**. Related-party status is "
                   "resolved by the retainer-counterparty fallback.",
}


def main(report=None, facts=None):
    OUT.mkdir(exist_ok=True)
    report = report or json.loads((ROOT / "compute_report.json").read_text(encoding="utf-8"))
    facts = facts or json.loads((ROOT / "facts.json").read_text(encoding="utf-8"))
    ix = json.loads((ROOT / "index.json").read_text(encoding="utf-8"))

    for sid, data in report.items():
        src = facts["sources"][sid]
        L = [f"# {sid} — covenant compliance", "",
             f"**Account:** `{ix['scenario_to_account'][sid]}`", "",
             "## Documents relied on", "", "| Role | File |", "|---|---|",
             f"| Credit agreement (live) | `{src['agreement']}.pdf` |"]
        for role, key in (("KYC dossier", "kyc"),
                          ("Financial statement notes", "financial_statements"),
                          ("Agreed-procedures report", "audit_notes"),
                          ("Treasury memo", "treasury_memo")):
            if src.get(key):
                L.append(f"| {role} | `{src[key]}.pdf` |")
        for d in src.get("discarded_drafts", []):
            L.append(f"| *(discarded)* interim draft | `{d}.pdf` |")

        L += ["", "## Adjustment trail", ""]
        L += [f"- {line}" for line in data["trail"]]

        for clause, c in sorted(data["cells"].items()):
            L += ["", f"## Clause {clause} — {c['metric']}", "",
                  "**Covenant text (verbatim):**", "",
                  "> " + c["covenant_text"].replace("\n", " "), "",
                  f"- **Threshold:** {c['threshold']} ({c['direction']}, {c['unit']})",
                  f"- **Computed metric:** `{c['raw']}`",
                  f"- **Emitted `actual`:** `{c['actual']}`",
                  f"- **Test:** {c['comparison']}",
                  f"- **Status:** `{c['status']}`",
                  "- **Evidence:** " + (f"`{c['evidence']}` — reverting this single row's "
                                        f"adjustment flips the verdict" if c["evidence"]
                                        else "`null` — no single transaction flips the "
                                             "verdict (ratio / aggregate test)")]
            if (sid, clause) in CAVEATS:
                L += ["", f"> **Caveat.** {CAVEATS[(sid, clause)]}"]

        (OUT / f"{sid}.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"wrote {len(report)} files to reasoning/")


if __name__ == "__main__":
    main()
