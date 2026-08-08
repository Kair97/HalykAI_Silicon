"""Agent entrypoint.

    python agent/run.py                 # full run -> agent/work/submission.json
    python agent/run.py --no-llm        # deterministic core only
    python agent/run.py --preflight     # check the API before committing to it

Order of operations is deliberate. The deterministic core runs FIRST and produces
a complete, valid submission. The LLM layer then improves specific cells. If the
API is slow, rate-limited or down, the run still ends with a submission on disk —
which matters more than any accuracy gain when the window is three hours long.
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import index_build          # noqa: E402
import llm                  # noqa: E402
import reason               # noqa: E402
import retrieve             # noqa: E402
from config import LLMConfig, ROOT, SUBMISSION, WORK  # noqa: E402

sys.path.insert(0, str(ROOT / "pipeline"))


RUNS = WORK / "runs"


def banner(t):
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def next_try_dir(explicit=None):
    """Every run lands in its own try{N}/ so results are never overwritten and
    two attempts can be compared side by side."""
    RUNS.mkdir(parents=True, exist_ok=True)
    if explicit is not None:
        d = RUNS / f"try{explicit}"
    else:
        used = [int(p.name[3:]) for p in RUNS.glob("try*") if p.name[3:].isdigit()]
        d = RUNS / f"try{max(used, default=0) + 1}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def as_key_shape(sub):
    """Same answers in the shape ground_truth.json uses, so a run can be diffed
    directly against a key. The SUBMISSION itself stays in template shape --
    that is the format the contest requires."""
    return {"scenarios": {sid: {"covenants": dict(cells)}
                          for sid, cells in sub["answers"].items()},
            "team": sub.get("team"), "model": sub.get("model")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--no-review", action="store_true",
                    help="skip the review pass (it is advisory and slow)")
    ap.add_argument("--try-number", type=int, default=None,
                    help="write into runs/try<N> instead of the next free number")
    ap.add_argument("--template", default=None,
                    help="path to submission_template.json (default: inside the archive)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    t0 = time.time()
    WORK.mkdir(parents=True, exist_ok=True)

    # The template defines WHICH borrowers and clauses to answer -- the agent
    # derives its whole target list from it and never assumes P1..P10/B1/B4.
    if a.template:
        src = Path(a.template).resolve()
        dst = ROOT / "agentic-bank-public" / "submission_template.json"
        if src != dst.resolve():
            shutil.copy(src, dst)
            print(f"using template {src}")

    if a.preflight:
        ok, msg = llm.preflight()
        print(("OK   " if ok else "FAIL ") + msg)
        raise SystemExit(0 if ok else 1)

    use_llm = not a.no_llm and LLMConfig.available()
    banner("Halyk covenant agent")
    print(LLMConfig.describe())
    if use_llm:
        ok, msg = llm.preflight()
        if not ok:
            print(f"  preflight failed -> continuing WITHOUT the LLM layer\n  {msg}")
            use_llm = False

    # ---- 1. one-time pre-index: after this nothing reads a PDF --------------
    banner("1. Pre-index documents")
    rules_index, chunks = index_build.build()
    retrieve.load(build_vectors=use_llm)

    # ---- 2. deterministic core: a complete, valid submission ---------------
    banner("2. Deterministic core")
    import p3_categorise
    import p4_compute
    txns, trail, facts = p3_categorise.build()

    unknown = {sid: [t for t in rows if t["category"] == "other"]
               for sid, rows in txns.items()}
    n_unknown = sum(len(v) for v in unknown.values())
    print(f"rule table placed "
          f"{sum(len(r) for r in txns.values()) - n_unknown} rows; "
          f"{n_unknown} unplaced")

    # ---- 3. LLM layer: only where the rules could not decide ---------------
    relabelled = 0
    if use_llm and n_unknown:
        banner("3. LLM categorisation of unplaced rows")
        for sid, rows in unknown.items():
            if not rows:
                continue
            cov = "\n".join(
                f"{cl}: {c['rule'][:220]}"
                for cl, c in rules_index["borrowers"][sid]["clauses"].items())
            got = reason.categorise_batch(
                [{"txn_id": t["txn_id"], "description": t["description"],
                  "counterparty": t["counterparty"]} for t in rows],
                covenant_context=cov)
            for t in rows:
                new = got.get(t["txn_id"])
                if new and new != "other":
                    t["category"] = new
                    t["notes"].append(f"category assigned by the LLM ({new}); "
                                      f"no rule pattern matched this wording")
                    trail[sid].append(f"llm-category: {t['txn_id']} -> {new}")
                    relabelled += 1
        print(f"relabelled {relabelled} of {n_unknown} unplaced rows")
    elif n_unknown:
        print(f"(LLM off: {n_unknown} unplaced rows contribute to no covenant)")

    # ---- 3b. one-off adjudication, only where a covenant adds items back ----
    reclassified_one_off = 0
    if use_llm:
        needs = {sid for sid in txns
                 for cl, c in rules_index["borrowers"][sid]["clauses"].items()
                 if c.get("metric_shape") == "adjusted_ebitda_margin"}
        if needs:
            banner("3b. LLM one-off / recurring adjudication")
        for sid in sorted(needs):
            opex = [t for t in txns[sid] if t["category"] == "opex"]
            got = reason.classify_one_off(
                [{"txn_id": t["txn_id"], "description": t["description"],
                  "counterparty": t["counterparty"]} for t in opex])
            for t in opex:
                if t["txn_id"] in got and got[t["txn_id"]] != t["one_off"]:
                    was = t["one_off"]
                    t["one_off"] = got[t["txn_id"]]
                    t["notes"].append(f"one-off status set to {t['one_off']} by the "
                                      f"LLM (rule said {was})")
                    trail[sid].append(f"llm-one-off: {t['txn_id']} {was} -> "
                                      f"{t['one_off']}")
                    reclassified_one_off += 1
            for t in opex:
                print(f"  {t['txn_id']:14} one_off={str(t['one_off']):5} "
                      f"{t['description'][:52]}")
        if needs:
            print(f"LLM changed {reclassified_one_off} one-off flag(s)")

    # ---- 4. compute, evidence, emit ---------------------------------------
    banner("4. Compute, evidence, emit")
    p3_categorise.build = lambda: (txns, trail, facts)      # reuse the adjusted set
    sub, report = p4_compute.main(strict=False)
    sub.update(SUBMISSION)

    # ---- 5. review pass over the finished verdicts ------------------------
    reviews = {}
    if use_llm and not a.no_review:
        banner("5. Review pass")
        flagged = 0
        for sid, data in report.items():
            for cl, cell in data["cells"].items():
                r = reason.review_cell(sid, cl, cell, trail=data["trail"])
                if not r:
                    continue
                reviews[f"{sid}/{cl}"] = r
                if not r["agree"]:
                    x = reason.cross_check(sid, cl, cell, data["trail"])
                    r["cross_check"] = x
                    # Two independent models objecting is worth a human's attention.
                    # One model objecting is not enough to overturn verified
                    # arithmetic, so we flag rather than flip.
                    flagged += 1
                    print(f"  ? {sid}/{cl}: {r['concern'][:150]}")
        print(f"reviewed {len(reviews)} cells, {flagged} flagged for attention")

    # ---- 6. write everything into this attempt's own directory ------------
    d = next_try_dir(a.try_number)
    meta = {
        "try": d.name, "llm_enabled": use_llm,
        "model": LLMConfig.model if use_llm else None,
        "review_pass": use_llm and not a.no_review,
        "llm_calls": llm.stats(), "rows_relabelled_by_llm": relabelled,
        "cells_reviewed": len(reviews),
        "cells_flagged": sum(1 for r in reviews.values() if not r["agree"]),
        "seconds": round(time.time() - t0, 1),
    }
    # the deliverable, in the shape the contest template requires
    (d / "submission.json").write_text(json.dumps(sub, indent=2), encoding="utf-8")
    # the same answers in ground_truth shape, for diffing against a key
    (d / "answers_key_shape.json").write_text(
        json.dumps(as_key_shape(sub), indent=2), encoding="utf-8")
    shutil.copy(ROOT / "compute_report.json", d / "compute_report.json")
    (d / "reviews.json").write_text(
        json.dumps(reviews, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "run_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # convenience copies at a stable path, always the latest attempt
    out = Path(a.out) if a.out else WORK / "submission.json"
    out.write_text(json.dumps(sub, indent=2), encoding="utf-8")

    banner("Done")
    cells = sum(len(v) for v in sub["answers"].values())
    print(f"attempt    : {d}")
    print(f"submission : {d / 'submission.json'}   ({cells} cells)")
    print(f"latest copy: {out}")
    print(f"llm calls  : {llm.stats()}")
    print(f"elapsed    : {time.time() - t0:.1f}s")

    key = ROOT / "agentic-bank-public" / "ground_truth.json"
    if key.exists():
        print()
        import score
        score.ROOT = ROOT
        shutil.copy(out, ROOT / "submission.json")
        score.main()
        meta["scored_against_key"] = True
        (d / "run_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    else:
        print("\n(no ground_truth.json in the archive -- nothing to score against, "
              "which is expected for the private dataset)")


if __name__ == "__main__":
    main()
