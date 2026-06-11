"""Merge two QualityStore files into one — exact Welford parallel merge.

Recovery tool for the 2026-06-11 quarantine incident: the main store was
quarantined (encrypted under .env's NCGA_DATA_KEY) while a key-less server
restart recorded new ratings into a fresh store (encrypted under the user
keyfile). Both files hold disjoint rating events; this merges them losslessly.

Each input is decrypted by trying, in order: NCGA_DATA_KEY, the user keyfile
(~/.local/share/ncga/data.key), plaintext. The output is encrypted with
resolve_key()'s normal resolution (NCGA_DATA_KEY first). Inputs are never
modified; the output path must not already exist.

Usage:
    python3 -m tools.merge_quality_stores STORE_A STORE_B OUT
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from native_chinese_assistant.crypto import (
    KeyMismatchError,
    _user_key_path,
    decrypt,
    encrypt,
    is_encrypted,
    resolve_key,
)


def load_store(path: Path) -> dict:
    blob = path.read_bytes()
    if not is_encrypted(blob):
        return json.loads(blob.decode("utf-8"))
    candidates = []
    if (key := resolve_key()) is not None:
        candidates.append(key)
    keyfile = _user_key_path()
    if keyfile.is_file() and (raw := keyfile.read_bytes()) not in candidates and len(raw) == 32:
        candidates.append(raw)
    for key in candidates:
        try:
            return json.loads(decrypt(blob, key))
        except KeyMismatchError:
            continue
    raise SystemExit(f"error: {path} decrypts with neither NCGA_DATA_KEY nor the keyfile")


def merge_stats(a: dict, b: dict) -> dict:
    """Chan et al. parallel-variance merge — exact, order-independent."""
    n = a["n"] + b["n"]
    delta = b["mean"] - a["mean"]
    mins = [x for x in (a["min"], b["min"]) if x is not None]
    maxs = [x for x in (a["max"], b["max"]) if x is not None]
    return {
        "n": n,
        "mean": a["mean"] + delta * b["n"] / n,
        "m2": a["m2"] + b["m2"] + delta * delta * a["n"] * b["n"] / n,
        "min": min(mins) if mins else None,
        "max": max(maxs) if maxs else None,
    }


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    a_path, b_path, out_path = (Path(p) for p in sys.argv[1:4])
    if out_path.exists():
        raise SystemExit(f"error: refusing to overwrite existing {out_path}")

    a, b = load_store(a_path), load_store(b_path)
    merged = dict(a)
    for bucket, payload in b.items():
        if bucket in merged:
            merged[bucket] = {"stats": merge_stats(merged[bucket]["stats"], payload["stats"])}
        else:
            merged[bucket] = payload

    key = resolve_key()
    if key is not None:
        out_path.write_bytes(
            encrypt(json.dumps(merged, ensure_ascii=False, separators=(",", ":")).encode(), key)
        )
    else:
        out_path.write_bytes(json.dumps(merged, ensure_ascii=False, indent=2).encode())
    os.chmod(out_path, 0o600)
    for bucket, payload in sorted(merged.items()):
        print(f"{bucket}: {json.dumps(payload['stats'], ensure_ascii=False)}")
    print(f"\nmerged {len(a)} + {len(b)} buckets -> {len(merged)} at {out_path}")


if __name__ == "__main__":
    main()
