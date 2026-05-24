#!/usr/bin/env python3
"""tools/import_lexicon.py

Incremental import of a lexicon.jsonl file (e.g. from Cowork deep research)
into data/lexicon.jsonl. Validates schema, dedupes on (variety, mandarin, local)
primary key, reports stats.

Usage:
    python3 tools/import_lexicon.py <incoming.jsonl> [--dry-run] [--out PATH]

Same shape as tools/import_corpus.py but for word-level mappings.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "data" / "lexicon.jsonl"

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

VALID_CATEGORIES = frozenset({
    "particle", "verb", "noun", "greeting", "idiom", "pronoun", "other"
})

REQUIRED_FIELDS = ("variety", "mandarin", "local")


def validate_entry(obj, line_no):
    if not isinstance(obj, dict):
        return False, None, f"line {line_no}: not a JSON object"
    for f in REQUIRED_FIELDS:
        if f not in obj or obj[f] is None or str(obj[f]).strip() == "":
            return False, None, f"line {line_no}: missing/empty field '{f}'"
    v = str(obj["variety"]).strip()
    if v not in VALID_VARIETIES:
        return False, None, f"line {line_no}: unknown variety '{v}'"
    mand = str(obj["mandarin"]).strip()
    local = str(obj["local"]).strip()
    if len(mand) < 1 or len(mand) > 30:
        return False, None, f"line {line_no}: mandarin length out of range"
    if len(local) < 1 or len(local) > 30:
        return False, None, f"line {line_no}: local length out of range"
    cat = str(obj.get("category", "other")).strip().lower()
    if cat not in VALID_CATEGORIES:
        cat = "other"
    normalized = {
        "variety": v,
        "mandarin": mand,
        "local": local,
        "category": cat,
    }
    for opt in ("ipa", "example_sentence", "source", "notes"):
        if obj.get(opt):
            normalized[opt] = str(obj[opt]).strip()
    return True, normalized, None


def existing_keys(out_path):
    keys = set()
    if not out_path.is_file():
        return keys
    for raw in out_path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        try:
            obj = json.loads(s)
            keys.add((
                str(obj["variety"]),
                str(obj["mandarin"]).strip(),
                str(obj["local"]).strip(),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return keys


def main():
    ap = argparse.ArgumentParser(description="Incremental lexicon.jsonl import")
    ap.add_argument("incoming")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    incoming = Path(args.incoming)
    if not incoming.is_file():
        print(f"✗ incoming file not found: {incoming}", file=sys.stderr)
        return 1

    out_path = Path(args.out)
    existing = existing_keys(out_path)
    print(f"data/lexicon.jsonl: {len(existing)} entries already")

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
        key = (normalized["variety"], normalized["mandarin"], normalized["local"])
        if key in existing:
            duplicates += 1
            continue
        existing.add(key)
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
        print("\nnothing to write")
        return 2

    out_path.parent.mkdir(parents=True, exist_ok=True)
    existing_text = out_path.read_text(encoding="utf-8") if out_path.is_file() else ""
    with tempfile.NamedTemporaryFile(
        mode="w", delete=False, dir=str(out_path.parent),
        prefix=".lexicon.", suffix=".jsonl.tmp", encoding="utf-8"
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
    print("\nReload server to rebuild LexiconRetriever index.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
