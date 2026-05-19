#!/usr/bin/env python3
"""Interactive CLI: review data/corpus_raw.jsonl and promote good ones to corpus.jsonl.

Usage:
    python3 tools/review_corpus.py                  # review all
    python3 tools/review_corpus.py --variety beijing_mandarin
    python3 tools/review_corpus.py --resume          # skip already-reviewed

Keys:
    y  → approve and promote to data/corpus.jsonl (quality_tier=verified)
    n  → reject (drops to data/corpus_rejected.jsonl for audit)
    e  → edit rewrite text inline, then approve
    s  → skip for now (stays in raw)
    q  → quit

Progress saved between sessions via data/corpus_review_cursor.txt.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
RAW_PATH = REPO_ROOT / "data" / "corpus_raw.jsonl"
APPROVED_PATH = REPO_ROOT / "data" / "corpus.jsonl"
REJECTED_PATH = REPO_ROOT / "data" / "corpus_rejected.jsonl"
CURSOR_PATH = REPO_ROOT / "data" / "corpus_review_cursor.txt"


def load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        try:
            out.append(json.loads(s))
        except json.JSONDecodeError:
            continue
    return out


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def rewrite_raw_without_index(idx: int, all_raw: list[dict]) -> None:
    """After approving/rejecting raw[idx], remove it from raw file."""
    remaining = [r for j, r in enumerate(all_raw) if j != idx]
    with RAW_PATH.open("w", encoding="utf-8") as f:
        for r in remaining:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variety", type=str, default="", help="filter to one variety")
    ap.add_argument("--resume", action="store_true", help="skip past last cursor")
    args = ap.parse_args()

    raw = load_jsonl(RAW_PATH)
    if not raw:
        print(f"No entries in {RAW_PATH}. Run tools/build_corpus.py first.")
        return 0

    start_at = 0
    if args.resume and CURSOR_PATH.is_file():
        try:
            start_at = int(CURSOR_PATH.read_text().strip())
        except ValueError:
            start_at = 0
        print(f"Resuming from index {start_at}")

    indices = list(range(start_at, len(raw)))
    if args.variety:
        indices = [i for i in indices if raw[i].get("variety") == args.variety]
    if not indices:
        print("Nothing to review.")
        return 0

    print(f"\n{len(indices)} entries to review. Keys: y/n/e/s/q\n")
    approved = rejected = skipped = 0

    for n, i in enumerate(indices, 1):
        item = raw[i]
        print(f"\n--- [{n}/{len(indices)}] index={i} ---")
        print(f"variety:  {item.get('variety')}")
        print(f"scenario: {item.get('scenario')}")
        print(f"原文:     {item.get('original')}")
        print(f"改写:     {item.get('rewrite')}")
        if item.get("notes"):
            print(f"notes:    {item['notes']}")

        while True:
            choice = input("[y/n/e/s/q] > ").strip().lower()
            if choice in ("y", "yes"):
                item["quality_tier"] = "verified"
                append_jsonl(APPROVED_PATH, item)
                rewrite_raw_without_index(i, raw)
                # IMPORTANT: re-load raw because we just rewrote the file
                raw = load_jsonl(RAW_PATH)
                # And re-compute remaining indices to skip stale i values
                approved += 1
                print("  ✓ approved")
                break
            if choice in ("n", "no"):
                append_jsonl(REJECTED_PATH, item)
                rewrite_raw_without_index(i, raw)
                raw = load_jsonl(RAW_PATH)
                rejected += 1
                print("  ✗ rejected (saved to corpus_rejected.jsonl)")
                break
            if choice in ("e", "edit"):
                print(f"current 改写: {item.get('rewrite')}")
                new_rewrite = input("new 改写 (blank=keep): ").strip()
                if new_rewrite:
                    item["rewrite"] = new_rewrite
                item["quality_tier"] = "verified"
                append_jsonl(APPROVED_PATH, item)
                rewrite_raw_without_index(i, raw)
                raw = load_jsonl(RAW_PATH)
                approved += 1
                print("  ✓ edited & approved")
                break
            if choice in ("s", "skip"):
                skipped += 1
                print("  ↪ skipped")
                break
            if choice in ("q", "quit"):
                CURSOR_PATH.write_text(str(i))
                print(f"\nSaved cursor at {i}. Resume with --resume.")
                print(f"Done so far: {approved} approved, {rejected} rejected, {skipped} skipped")
                return 0
            print("  (key not recognized — y/n/e/s/q)")

    CURSOR_PATH.write_text("0")
    print(f"\n✓ Review complete: {approved} approved, {rejected} rejected, {skipped} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
