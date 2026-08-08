"""Regenerate every deliverable from the raw archive in one command.

    python pipeline/run_all.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import p0_extract, p1_triage, p2_facts, p4_compute, p5_reasoning  # noqa: E402

ROOT = Path(os.environ.get("HALYK_ROOT", Path(__file__).resolve().parent.parent))
ARCHIVE = Path(os.environ.get("HALYK_ARCHIVE", ROOT / "agentic-bank-public"))


def main():
    for name, fn in (("Phase 0  extract", p0_extract.main),
                     ("Phase 1-2 triage + entity resolution", p1_triage.main),
                     ("Phase 3  parse covenants / KYC / notes", p2_facts.main)):
        print(f"\n=== {name}")
        fn()

    print("\n=== Phase 4-8  categorise, adjust, compute, evidence, emit")
    sub, report = p4_compute.main(strict=False)
    p5_reasoning.main(report)

    if (ARCHIVE / "ground_truth.json").exists():
        print("\n=== dev-set score (validation only; never an input)")
        import score
        score.main()


if __name__ == "__main__":
    main()
