#!/usr/bin/env python3
"""Interactive CLI: review data/corpus_raw.jsonl and promote good ones to corpus.jsonl.

Usage:
    python3 tools/review_corpus.py                  # review all
    python3 tools/review_corpus.py --variety beijing_mandarin
    python3 tools/review_corpus.py --resume          # skip past last cursor

Keys:
    y  → approve and promote to data/corpus.jsonl (quality_tier=verified)
    n  → reject (drops to data/corpus_rejected.jsonl for audit)
    e  → edit rewrite text inline, then approve
    s  → skip for now (stays in raw)
    q  → quit

Design (rewritten after the stale-index bug):
- The review loop iterates over a SNAPSHOT of raw.jsonl taken once at start.
- Decisions are collected keyed by (variety, original) — no positional indices.
- All files are written ONCE at quit/completion: approved entries are appended
  to corpus.jsonl, rejected ones to corpus_rejected.jsonl, and raw.jsonl is
  rewritten atomically (tempfile+rename, same pattern as import_corpus.py)
  without the decided entries.
- Approvals are validated against import_corpus.validate_entry (variety and
  scenario enums) before promotion; invalid entries stay in raw.
- The resume cursor is the (variety, original) key of the entry on screen at
  quit time (stored as JSON in data/corpus_review_cursor.txt), not an index.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_PATH = REPO_ROOT / "data" / "corpus_raw.jsonl"
APPROVED_PATH = REPO_ROOT / "data" / "corpus.jsonl"
REJECTED_PATH = REPO_ROOT / "data" / "corpus_rejected.jsonl"
CURSOR_PATH = REPO_ROOT / "data" / "corpus_review_cursor.txt"


def _load_import_corpus():
    """Load tools/import_corpus.py for its canonical validate_entry/existing_keys."""
    spec = importlib.util.spec_from_file_location(
        "ncga_import_corpus", Path(__file__).resolve().parent / "import_corpus.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_import_corpus = _load_import_corpus()
validate_entry = _import_corpus.validate_entry
existing_keys = _import_corpus.existing_keys


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


def entry_key(item: dict) -> tuple[str, str]:
    """Content key — same (variety, original) primary key import_corpus uses."""
    return (str(item.get("variety", "")), str(item.get("original", "")).strip())


def write_jsonl_atomic(path: Path, rows: list[dict]) -> None:
    """Rewrite a JSONL file via tempfile+rename (same pattern as import_corpus.py)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        encoding="utf-8",
    ) as tf:
        for r in rows:
            tf.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp_path = Path(tf.name)
    tmp_path.replace(path)


def append_jsonl_atomic(path: Path, rows: list[dict]) -> None:
    """Append rows by rewriting the whole file via tempfile+rename."""
    existing_text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if existing_text and not existing_text.endswith("\n"):
        existing_text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        encoding="utf-8",
    ) as tf:
        tf.write(existing_text)
        for r in rows:
            tf.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp_path = Path(tf.name)
    tmp_path.replace(path)


def apply_decisions(
    raw_entries: list[dict], decisions: dict[tuple[str, str], tuple[str, dict]]
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split a snapshot of raw entries by decision.

    decisions maps (variety, original) → ("approve", normalized) | ("reject", entry).
    Returns (remaining, approved, rejected). Undecided entries stay in remaining.
    Pure content-key logic — no indices, so nothing can go stale or out of range.
    """
    remaining: list[dict] = []
    approved: list[dict] = []
    rejected: list[dict] = []
    for item in raw_entries:
        decision = decisions.get(entry_key(item))
        if decision is None:
            remaining.append(item)
        elif decision[0] == "approve":
            approved.append(decision[1])
        else:
            rejected.append(decision[1])
    return remaining, approved, rejected


def _read_cursor() -> tuple[str, str] | None:
    if not CURSOR_PATH.is_file():
        return None
    try:
        obj = json.loads(CURSOR_PATH.read_text(encoding="utf-8"))
        return (str(obj["variety"]), str(obj["original"]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _finalize(raw: list[dict], decisions: dict) -> tuple[int, int]:
    """Write all three files once. Returns (promoted, duplicate) counts."""
    remaining, approved, rejected = apply_decisions(raw, decisions)
    if rejected:
        append_jsonl_atomic(REJECTED_PATH, rejected)
    promoted = duplicates = 0
    if approved:
        have = existing_keys(APPROVED_PATH)
        new_rows = []
        for item in approved:
            k = entry_key(item)
            if k in have:
                duplicates += 1
                continue
            have.add(k)
            new_rows.append(item)
        promoted = len(new_rows)
        if new_rows:
            append_jsonl_atomic(APPROVED_PATH, new_rows)
    write_jsonl_atomic(RAW_PATH, remaining)
    return promoted, duplicates


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variety", type=str, default="", help="filter to one variety")
    ap.add_argument("--resume", action="store_true", help="skip past last cursor")
    args = ap.parse_args(argv)

    # SNAPSHOT: raw.jsonl is read once here and not touched until _finalize.
    raw = load_jsonl(RAW_PATH)
    if not raw:
        print(f"No entries in {RAW_PATH}. Run tools/build_corpus.py first.")
        return 0

    queue = list(raw)
    if args.variety:
        queue = [it for it in queue if it.get("variety") == args.variety]

    if args.resume:
        cursor_key = _read_cursor()
        if cursor_key is not None:
            keys = [entry_key(it) for it in queue]
            if cursor_key in keys:
                pos = keys.index(cursor_key)
                queue = queue[pos:]  # cursor entry was undecided at quit — re-show it
                print(f"Resuming at {cursor_key[0]} · {cursor_key[1][:20]}")

    if not queue:
        print("Nothing to review.")
        return 0

    print(f"\n{len(queue)} entries to review. Keys: y/n/e/s/q\n")
    decisions: dict[tuple[str, str], tuple[str, dict]] = {}
    approved = rejected = skipped = invalid = 0
    quit_key: tuple[str, str] | None = None

    for n, item in enumerate(queue, 1):
        print(f"\n--- [{n}/{len(queue)}] ---")
        print(f"variety:  {item.get('variety')}")
        print(f"scenario: {item.get('scenario')}")
        print(f"原文:     {item.get('original')}")
        print(f"改写:     {item.get('rewrite')}")
        if item.get("notes"):
            print(f"notes:    {item['notes']}")

        while True:
            choice = input("[y/n/e/s/q] > ").strip().lower()
            if choice in ("y", "yes", "e", "edit"):
                candidate = dict(item)
                if choice in ("e", "edit"):
                    print(f"current 改写: {candidate.get('rewrite')}")
                    new_rewrite = input("new 改写 (blank=keep): ").strip()
                    if new_rewrite:
                        candidate["rewrite"] = new_rewrite
                candidate["quality_tier"] = "verified"
                ok, normalized, reason = validate_entry(candidate, n)
                if not ok:
                    print(f"  ✗ cannot promote — {reason} (kept in raw)")
                    invalid += 1
                    break
                decisions[entry_key(item)] = ("approve", normalized)
                approved += 1
                print("  ✓ approved" if choice in ("y", "yes") else "  ✓ edited & approved")
                break
            if choice in ("n", "no"):
                decisions[entry_key(item)] = ("reject", item)
                rejected += 1
                print("  ✗ rejected (saved to corpus_rejected.jsonl)")
                break
            if choice in ("s", "skip"):
                skipped += 1
                print("  ↪ skipped")
                break
            if choice in ("q", "quit"):
                quit_key = entry_key(item)
                break
            print("  (key not recognized — y/n/e/s/q)")

        if quit_key is not None:
            break

    promoted, duplicates = _finalize(raw, decisions)

    if quit_key is not None:
        CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        CURSOR_PATH.write_text(
            json.dumps({"variety": quit_key[0], "original": quit_key[1]}, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nSaved cursor at {quit_key[0]} · {quit_key[1][:20]}. Resume with --resume.")
    elif CURSOR_PATH.is_file():
        CURSOR_PATH.unlink()

    summary = f"{approved} approved ({promoted} promoted"
    if duplicates:
        summary += f", {duplicates} already in corpus"
    summary += f"), {rejected} rejected, {skipped} skipped"
    if invalid:
        summary += f", {invalid} invalid (kept in raw)"
    if quit_key is not None:
        print(f"Done so far: {summary}")
    else:
        print(f"\n✓ Review complete: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
