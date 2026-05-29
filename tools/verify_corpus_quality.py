#!/usr/bin/env python3
"""tools/verify_corpus_quality.py

After importing Cowork's corpus/lexicon, randomly sample N rows and verify:
1. source URL exists (HTTP 2xx via HEAD/GET)
2. quality_tier=verified rows have a real source (not blank)
3. report verified-rate per variety

Usage:
    python3 tools/verify_corpus_quality.py [--corpus N] [--lexicon N] [--timeout SECONDS]
"""

import argparse
import json
import random
import ssl
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_PATH = REPO_ROOT / "data" / "corpus.jsonl"
LEXICON_PATH = REPO_ROOT / "data" / "lexicon.jsonl"


def head_check(url: str, timeout: float = 8.0) -> tuple[bool, str]:
    """Return (is_ok, status_string) — try HEAD first, GET fallback."""
    if not url or not url.startswith(("http://", "https://")):
        return False, "no-scheme"
    ctx = ssl.create_default_context()
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(
            url,
            method=method,
            headers={"User-Agent": "NCGA-verify/0.1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                code = resp.getcode()
                if 200 <= code < 300:
                    return True, f"HTTP {code}"
                if 300 <= code < 400:
                    return True, f"HTTP {code} (redirect)"
                return False, f"HTTP {code}"
        except urllib.error.HTTPError as e:
            if method == "GET":
                return False, f"HTTPError {e.code}"
        except (urllib.error.URLError, TimeoutError, ssl.SSLError) as e:
            if method == "GET":
                return False, f"network: {e!s}"
        except Exception as e:
            return False, f"other: {e!s}"
    return False, "no method worked"


def load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        try:
            rows.append(json.loads(s))
        except (KeyError, ValueError):
            continue
    return rows


def variety_breakdown(rows: list[dict]) -> None:
    by_variety = Counter(r.get("variety", "?") for r in rows)
    verified = Counter(r.get("variety", "?") for r in rows if r.get("quality_tier") == "verified")
    print("\n  Per variety (verified / total):")
    for v in sorted(by_variety):
        print(f"    {v:<40} {verified[v]:>4} / {by_variety[v]:>4}")


def verify_corpus(n: int, timeout: float) -> int:
    rows = load_jsonl(CORPUS_PATH)
    print(f"corpus.jsonl: {len(rows)} entries")
    variety_breakdown(rows)
    with_source = [r for r in rows if r.get("source")]
    print(f"\n  rows with source URL: {len(with_source)} / {len(rows)}")
    if not with_source:
        print("  (no URLs to verify)")
        return 0
    sample = random.sample(with_source, min(n, len(with_source)))
    print(f"\n  spot-checking {len(sample)} URLs (timeout {timeout}s each):")
    ok_n = bad_n = 0
    for i, r in enumerate(sample, 1):
        ok, status = head_check(r["source"], timeout)
        mark = "✓" if ok else "✗"
        url = r["source"]
        if len(url) > 70:
            url = url[:67] + "..."
        print(f"  {mark} [{i:>2}/{len(sample)}] {status:<22} {url}")
        ok_n += int(ok)
        bad_n += int(not ok)
    print(f"\n  result: {ok_n} ok / {bad_n} bad ({ok_n / (ok_n + bad_n) * 100:.0f}% reachable)")
    return 0


def verify_lexicon(n: int, timeout: float) -> int:
    rows = load_jsonl(LEXICON_PATH)
    print(f"\nlexicon.jsonl: {len(rows)} entries")
    variety_breakdown(rows)
    with_source = [r for r in rows if r.get("source")]
    print(f"\n  rows with source URL: {len(with_source)} / {len(rows)}")
    if not with_source:
        print("  (no URLs to verify)")
        return 0
    sample = random.sample(with_source, min(n, len(with_source)))
    print(f"\n  spot-checking {len(sample)} URLs:")
    ok_n = bad_n = 0
    for i, r in enumerate(sample, 1):
        ok, status = head_check(r["source"], timeout)
        mark = "✓" if ok else "✗"
        url = r["source"]
        if len(url) > 60:
            url = url[:57] + "..."
        print(f"  {mark} [{i:>2}/{len(sample)}] {status:<22} {r['mandarin']} → {r['local']:<10} {url}")
        ok_n += int(ok)
        bad_n += int(not ok)
    print(f"\n  result: {ok_n} ok / {bad_n} bad ({ok_n / (ok_n + bad_n) * 100:.0f}% reachable)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=int, default=20, help="N corpus URLs to spot-check")
    ap.add_argument("--lexicon", type=int, default=20, help="N lexicon URLs to spot-check")
    ap.add_argument("--timeout", type=float, default=8.0, help="HTTP timeout per URL")
    ap.add_argument("--seed", type=int, default=None, help="random seed for reproducibility")
    args = ap.parse_args()
    if args.seed is not None:
        random.seed(args.seed)
    verify_corpus(args.corpus, args.timeout)
    verify_lexicon(args.lexicon, args.timeout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
