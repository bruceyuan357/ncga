#!/usr/bin/env python3
"""tools/import_corpus.py

Incremental import of a corpus.jsonl file (e.g. from Cowork deep research)
into data/corpus.jsonl. Validates schema, dedupes against existing entries
on (variety, original) primary key, and reports stats.

Usage:
    python3 tools/import_corpus.py <incoming.jsonl> [--dry-run] [--out PATH]

Behavior:
- Reads incoming JSONL line by line
- Validates each entry has required fields (variety/scenario/original/rewrite/quality_tier)
- Validates variety is one of the 10 known keys
- Validates scenario is one of the 10 known keys
- Skips lines that already exist in data/corpus.jsonl by (variety, original) key
- Appends survivors to data/corpus.jsonl (atomic write via tempfile+rename)
- Prints stats: total / valid / duplicate / rejected (with reason for each reject)

Exit codes:
    0  — success
    1  — no incoming file / unreadable
    2  — schema-level failures only (no rows actually imported but file was OK)
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "data" / "corpus.jsonl"

VALID_VARIETIES = frozenset({
    "standard_putonghua",
    "beijing_mandarin",
    "dongbei_mandarin",
    "sichuan_chongqing_mandarin",
    "jianghuai_or_lower_yangtze_mandarin",
    "guangdong_mandarin",
    "shanghai_mandarin_style",
    "cantonese_written",
    "hokkien_written",
    "minnan_written",
})

VALID_SCENARIOS = frozenset({
    "complaint", "request", "praise", "greeting", "small_talk",
    "self_depreciate", "teach_kid", "comfort", "invite", "apology",
})

REQUIRED_FIELDS = ("variety", "scenario", "original", "rewrite", "quality_tier")


def validate_entry(obj, line_no):
    """Return (is_valid, normalized_dict, reason). Reason only set when invalid."""
    if not isinstance(obj, dict):
        return False, None, f"line {line_no}: not a JSON object"
    for f in REQUIRED_FIELDS:
        if f not in obj or obj[f] is None or str(obj[f]).strip() == "":
            return False, None, f"line {line_no}: missing/empty field '{f}'"
    v = str(obj["variety"]).strip()
    if v not in VALID_VARIETIES:
        return False, None, f"line {line_no}: unknown variety '{v}'"
    s = str(obj["scenario"]).strip()
    if s not in VALID_SCENARIOS:
        return False, None, f"line {line_no}: unknown scenario '{s}'"
    qt = str(obj["quality_tier"]).strip()
    if qt not in ("verified", "needs_review"):
        # Be permissive: coerce to needs_review with a warning rather than reject
        qt = "needs_review"
    # Length sanity: avoid empty original / rewrite
    if len(str(obj["original"])) < 2:
        return False, None, f"line {line_no}: original too short"
    if len(str(obj["rewrite"])) < 2:
        return False, None, f"line {line_no}: rewrite too short"
    normalized = {
        "variety": v,
        "scenario": s,
        "original": str(obj["original"]).strip(),
        "rewrite": str(obj["rewrite"]).strip(),
        "quality_tier": qt,
        "notes": str(obj.get("notes", "")),
    }
    # Preserve optional source field if present (Cowork's research output has it)
    if obj.get("source"):
        normalized["source"] = str(obj["source"]).strip()
    return True, normalized, None


def existing_keys(out_path):
    """Read data/corpus.jsonl and return set of (variety, original) primary keys."""
    keys = set()
    if not out_path.is_file():
        return keys
    for raw in out_path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        try:
            obj = json.loads(s)
            keys.add((str(obj["variety"]), str(obj["original"]).strip()))
        except (KeyError, ValueError, TypeError):
            continue
    return keys


def main():
    ap = argparse.ArgumentParser(description="Incremental corpus.jsonl import")
    ap.add_argument("incoming", help="Path to incoming JSONL file")
    ap.add_argument("--dry-run", action="store_true", help="Validate only, don't write")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help=f"Output corpus path (default: {DEFAULT_OUT})")
    args = ap.parse_args()

    incoming = Path(args.incoming)
    if not incoming.is_file():
        print(f"✗ incoming file not found: {incoming}", file=sys.stderr)
        return 1

    out_path = Path(args.out)
    existing = existing_keys(out_path)
    print(f"data/corpus.jsonl: {len(existing)} entries already")

    total = 0
    valid_new = []
    rejects = []
    duplicates = 0

    for line_no, raw in enumerate(incoming.read_text(encoding="utf-8").splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        total += 1
        try:
            obj = json.loads(s)
        except ValueError as e:
            rejects.append(f"line {line_no}: JSON parse error — {e}")
            continue
        ok, normalized, reason = validate_entry(obj, line_no)
        if not ok:
            rejects.append(reason)
            continue
        key = (normalized["variety"], normalized["original"])
        if key in existing:
            duplicates += 1
            continue
        existing.add(key)         # prevent intra-file dupes too
        valid_new.append(normalized)

    print(f"incoming total:    {total}")
    print(f"valid + new:       {len(valid_new)}")
    print(f"duplicate (skipped): {duplicates}")
    print(f"rejected:          {len(rejects)}")
    for r in rejects[:20]:
        print(f"   ✗ {r}")
    if len(rejects) > 20:
        print(f"   ... and {len(rejects) - 20} more")

    if args.dry_run:
        print("\n[dry-run] no file written")
        return 0 if valid_new else 2
    if not valid_new:
        print("\nnothing to write (no new valid entries)")
        return 2

    # Atomic append: read+rewrite to tempfile then rename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    existing_text = out_path.read_text(encoding="utf-8") if out_path.is_file() else ""
    with tempfile.NamedTemporaryFile(
        mode="w", delete=False, dir=str(out_path.parent),
        prefix=".corpus.", suffix=".jsonl.tmp", encoding="utf-8"
    ) as tf:
        tf.write(existing_text)
        if existing_text and not existing_text.endswith("\n"):
            tf.write("\n")
        for entry in valid_new:
            tf.write(json.dumps(entry, ensure_ascii=False) + "\n")
        tmp_path = Path(tf.name)
    tmp_path.replace(out_path)

    print(f"\n✓ {len(valid_new)} new entries appended to {out_path}")
    print(f"  total now: {len(existing)} (was {len(existing) - len(valid_new)})")
    print("\nReload server (python3 app.py stop && python3 app.py) to rebuild BM25 index.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
