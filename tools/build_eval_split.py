#!/usr/bin/env python3
"""Build the golden eval split (Stage 1 of the eval protocol).

Selects one verified row per (variety, scenario) from data/corpus.jsonl —
deterministic (first row in file order), reproducible (re-run any time) —
and writes data/eval_golden.jsonl. These rows are FROZEN ground truth:

  - they anchor the reference-based judge in tools/eval_dialects.py
  - corpus.get_default_retriever() excludes them from few-shot injection
    (contamination guard — a row can't both teach and grade)

standard_putonghua contributes nothing today (0 verified rows); backfill via
tools/build_corpus.py + review, then re-run this script.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "data" / "corpus.jsonl"
OUT = ROOT / "data" / "eval_golden.jsonl"


def main() -> int:
    seen: set[tuple[str, str]] = set()
    picked: list[dict] = []
    verified_total = 0
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        entry = json.loads(s)
        if entry.get("quality_tier") != "verified":
            continue
        verified_total += 1
        key = (entry["variety"], entry["scenario"])
        if key in seen:
            continue
        seen.add(key)
        picked.append(
            {
                "variety": entry["variety"],
                "scenario": entry["scenario"],
                "original": entry["original"],
                "reference_rewrite": entry["rewrite"],
            }
        )
    with OUT.open("w", encoding="utf-8") as fh:
        fh.write("# Golden eval split — frozen ground truth for tools/eval_dialects.py.\n")
        fh.write("# Regenerate: python3 tools/build_eval_split.py (do NOT edit rows by hand;\n")
        fh.write("# fix the corpus + review first, then rebuild).\n")
        for entry in picked:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    varieties = {e["variety"] for e in picked}
    print(f"wrote {OUT.name}: {len(picked)} rows across {len(varieties)} varieties (from {verified_total} verified corpus rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
