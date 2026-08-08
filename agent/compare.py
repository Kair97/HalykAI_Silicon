"""Compare every attempt in agent/work/runs/.

    python agent/compare.py            # table of all tries
    python agent/compare.py try1 try3  # cell-by-cell diff between two tries

With a ground_truth.json in the archive each try is scored; without one (the real
private dataset) it still shows how many cells changed between attempts, which is
the only signal available when there is no key.
"""
import json
import sys
from pathlib import Path

from config import ROOT, WORK

RUNS = WORK / "runs"


def load(try_name):
    p = RUNS / try_name / "submission.json"
    if not p.exists():
        raise SystemExit(f"no such attempt: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def key():
    p = ROOT / "agentic-bank-public" / "ground_truth.json"
    if not p.exists():
        return None
    gt = json.loads(p.read_text(encoding="utf-8"))
    return {(sid, cl): c for sid, s in gt["scenarios"].items()
            for cl, c in s["covenants"].items()}


def score(sub, k):
    total = n = statuses = 0.0
    bad = []
    for (sid, cl), want in k.items():
        got = sub["answers"].get(sid, {}).get(cl)
        n += 1
        if not got or got["status"] != want["status"]:
            bad.append((f"{sid}/{cl}", "STATUS",
                        f"{(got or {}).get('status')} != {want['status']}"))
            continue
        statuses += 1
        kv = abs(float(want["actual"]))
        e = abs(float(got["actual"]) - kv) / kv if kv else 0
        s = 0.50 + 0.30 * max(0.0, 1 - e / 0.05)
        if want["evidence_txn_id"] is None:
            s += 0.20 * max(0.0, 1 - e / 0.05)
        elif got["evidence_txn_id"] == want["evidence_txn_id"]:
            s += 0.20
        total += s
        if s < 0.999:
            bad.append((f"{sid}/{cl}", "ACTUAL",
                        f"{got['actual']} vs {want['actual']} ({e*100:.1f}%)"))
    return total, n, statuses, bad


def table():
    tries = sorted(RUNS.glob("try*"), key=lambda p: int(p.name[3:]))
    if not tries:
        raise SystemExit("no attempts yet -- run: python agent/run.py")
    k = key()
    print(f"{'try':7} {'llm':>4} {'review':>7} {'calls':>6} {'flag':>5} "
          f"{'secs':>7} {'statuses':>9} {'score':>16}")
    print("-" * 78)
    for t in tries:
        m = json.loads((t / "run_meta.json").read_text(encoding="utf-8"))
        line = (f"{t.name:7} {'yes' if m['llm_enabled'] else 'no':>4} "
                f"{'yes' if m.get('review_pass') else 'no':>7} "
                f"{m['llm_calls']['chat']:>6} {m['cells_flagged']:>5} "
                f"{m['seconds']:>7.1f}")
        if k:
            tot, n, st, _ = score(load(t.name), k)
            line += f" {int(st):>4}/{int(n):<4} {tot:>8.3f}/{int(n):<6}"
        else:
            line += f" {'-':>9} {'no key':>16}"
        print(line)
    if k:
        print("\nimperfect cells in the latest attempt:")
        _t, _n, _s, bad = score(load(tries[-1].name), k)
        for cell, kind, why in bad:
            print(f"  {cell:9} {kind:7} {why}")
        if not bad:
            print("  none")


def diff(a, b):
    sa, sb = load(a), load(b)
    k = key()
    changed = 0
    print(f"{'cell':9} {'field':9} {a:>22} {b:>22}" + ("   key" if k else ""))
    print("-" * (64 + (24 if k else 0)))
    for sid, cells in sa["answers"].items():
        for cl in cells:
            x, y = sa["answers"][sid][cl], sb["answers"].get(sid, {}).get(cl, {})
            for f in ("status", "actual", "evidence_txn_id"):
                if x.get(f) != y.get(f):
                    changed += 1
                    kv = ""
                    if k:
                        kv = f"   {k.get((sid, cl), {}).get(f)}"
                    print(f"{sid+'/'+cl:9} {f:9} {str(x.get(f)):>22} "
                          f"{str(y.get(f)):>22}{kv}")
    print(f"\n{changed} field(s) differ")


if __name__ == "__main__":
    if len(sys.argv) == 3:
        diff(sys.argv[1], sys.argv[2])
    else:
        table()
