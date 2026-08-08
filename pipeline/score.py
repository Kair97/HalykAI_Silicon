"""Dev-set scorer. Grades submission.json against the ground_truth.json that
ships inside the public archive, using the scoring rule from CASE.ru.md.

This file exists to VALIDATE the pipeline. It is never an input to it -- no
value in submission.json is read from, or fitted to, the key.
"""
import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("HALYK_ROOT",
                           Path(__file__).resolve().parent.parent))
ARCHIVE = Path(os.environ.get("HALYK_ARCHIVE", ROOT / "agentic-bank-public"))


def cell_score(got, key):
    if got["status"] != key["status"]:
        return 0.0, "status wrong -> whole cell 0"
    pts, why = 0.50, "status"
    kv, gv = abs(float(key["actual"])), float(got["actual"])
    e = abs(gv - kv) / kv if kv else (0 if gv == 0 else 1)
    a = 0.30 * max(0.0, 1 - e / 0.05)
    pts += a
    why += f" +actual {a:.3f} (err {e*100:.2f}%)"
    if key["evidence_txn_id"] is None:
        ride = 0.20 * max(0.0, 1 - e / 0.05)
        pts += ride
        why += f" +evidence-rides-actual {ride:.3f}"
    else:
        hit = 0.20 if got["evidence_txn_id"] == key["evidence_txn_id"] else 0.0
        pts += hit
        why += f" +evidence {hit:.2f} (key {key['evidence_txn_id']}, got {got['evidence_txn_id']})"
    return pts, why


def main():
    sub = json.loads((ROOT / "submission.json").read_text(encoding="utf-8"))
    gt = json.loads((ARCHIVE / "ground_truth.json").read_text(encoding="utf-8"))
    tot = n = 0.0
    bad = []
    print(f"{'cell':10} {'status':22} {'actual (got / key)':40} {'score'}")
    print("-" * 96)
    for sid, sc in gt["scenarios"].items():
        for cl, key in sc["covenants"].items():
            got = sub["answers"][sid][cl]
            s, why = cell_score(got, key)
            tot += s
            n += 1
            flag = "" if s > 0.99 else ("  <-- STATUS" if got["status"] != key["status"]
                                        else "  <-- actual")
            print(f"{sid+'/'+cl:10} {got['status']:10}/{key['status']:10} "
                  f"{got['actual']:>18,.2f} / {float(key['actual']):>17,.2f} {s:6.3f}{flag}")
            if s < 0.999:
                bad.append((f"{sid}/{cl}", round(s, 3), why))
    print("-" * 96)
    statuses = sum(1 for sid, sc in gt["scenarios"].items() for cl, k in sc["covenants"].items()
                   if sub["answers"][sid][cl]["status"] == k["status"])
    print(f"statuses correct : {statuses}/{n:.0f}  ({statuses/n*100:.1f}%)")
    print(f"total score      : {tot:.3f} / {n:.0f}  ({tot/n*100:.1f}%)")
    if bad:
        print("\nimperfect cells:")
        for c, s, why in bad:
            print(f"  {c:9} {s:6.3f}  {why}")


if __name__ == "__main__":
    main()
