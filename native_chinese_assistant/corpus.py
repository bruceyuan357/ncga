"""Few-shot corpus + BM25 retriever (zero-dep, ~120 lines).

Cycle 22 Stage C: hand-written corpus of (variety, scenario, original, rewrite)
pairs. When the user asks to rewrite text, we BM25-retrieve the top-k most
similar examples for the target variety and inject them into the system
prompt as "学一下这些本地人怎么说" few-shot examples.

Why BM25 instead of sentence embeddings:
- No heavy ML deps (consistent with NCGA's minimal-deps rule: only certifi +
  cryptography at runtime). sentence-transformers + torch would be ~1GB on first import.
- For short text (<50 chars), BM25 with char-bigram tokenization performs
  competitively with dense retrieval. The downside is poor synonym handling,
  but our corpus is small enough that any missed match falls back to "use any
  3 from this variety" rather than embarrassing the model.
- If quality is insufficient, swap-in a dense retriever via a 2-method class
  with the same retrieve() signature.

Public API:
    load_corpus(path) -> list[CorpusEntry]
    BM25Retriever(entries) -> .retrieve(query, variety, top_k=3)
    format_examples_for_prompt(entries) -> list[str]  # per-line lines
    get_default_retriever() -> module-level singleton, loads data/corpus.jsonl
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# Path to the corpus JSONL. Override via NCGA_CORPUS_PATH for tests.
_DEFAULT_CORPUS_PATH = Path(__file__).parent.parent / "data" / "corpus.jsonl"


@dataclass(frozen=True)
class CorpusEntry:
    """One labeled example: source text + target-variety rewrite."""

    variety: str
    scenario: str
    original: str
    rewrite: str
    quality_tier: str
    notes: str = ""


def load_corpus(path: str | Path | None = None) -> list[CorpusEntry]:
    """Load corpus from JSONL. Returns empty list if file missing.

    Skips malformed lines silently (logs would be nice but we keep zero deps).
    """
    p = Path(path) if path else _DEFAULT_CORPUS_PATH
    if not p.is_file():
        return []
    entries: list[CorpusEntry] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        try:
            obj = json.loads(s)
            entries.append(
                CorpusEntry(
                    variety=str(obj["variety"]),
                    scenario=str(obj["scenario"]),
                    original=str(obj["original"]),
                    rewrite=str(obj["rewrite"]),
                    quality_tier=str(obj.get("quality_tier", "verified")),
                    notes=str(obj.get("notes", "")),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return entries


# ---------- BM25 ----------

# Tokenizer: split on whitespace + non-CJK punctuation, AND emit each CJK
# character + character-bigram as its own token. This captures both Latin word
# matches (for English/numbers in queries) and Chinese substring overlap.
_NON_TOKEN_RE = re.compile(r"[\s,，。！？!?;；:：、（）()\[\]【】《》<>\"'`~]+")


def _tokenize(text: str) -> list[str]:
    """Tokenize for BM25: ASCII words (lowercased) + CJK chars + CJK char-bigrams.

    Walks the string accumulating either a CJK run or an ASCII run; flushes
    whichever just ended when the char type flips. ASCII flush emits the whole
    word as one token (lowercased). CJK flush emits each char + each adjacent
    bigram so short Chinese inputs have enough surface overlap.
    """
    if not text:
        return []
    tokens: list[str] = []
    for chunk in _NON_TOKEN_RE.split(text):
        if not chunk:
            continue
        cjk_run: list[str] = []
        ascii_run: list[str] = []
        for ch in chunk:
            cp = ord(ch)
            is_cjk = 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0xF900 <= cp <= 0xFAFF
            if is_cjk:
                if ascii_run:
                    tokens.append("".join(ascii_run).lower())
                    ascii_run = []
                cjk_run.append(ch)
            else:
                if cjk_run:
                    _emit_cjk(cjk_run, tokens)
                    cjk_run = []
                ascii_run.append(ch)
        if cjk_run:
            _emit_cjk(cjk_run, tokens)
        if ascii_run:
            tokens.append("".join(ascii_run).lower())
    return tokens


def _emit_cjk(chars: list[str], out: list[str]) -> None:
    """Emit each char + each adjacent bigram (helps phrase overlap)."""
    for ch in chars:
        out.append(ch)
    for i in range(len(chars) - 1):
        out.append(chars[i] + chars[i + 1])


class BM25Retriever:
    """Tiny BM25 over a fixed corpus, indexed by variety.

    Usage:
        r = BM25Retriever(load_corpus())
        hits = r.retrieve("你能帮我个忙吗", variety="shanghai_mandarin_style", top_k=3)
    """

    K1 = 1.5
    B = 0.75

    def __init__(self, entries: Iterable[CorpusEntry]):
        # Group by variety so retrieve() only scores within the right pool
        self._by_variety: dict[str, list[CorpusEntry]] = {}
        for e in entries:
            self._by_variety.setdefault(e.variety, []).append(e)
        # Pre-compute per-variety: doc tokens, doc length, IDF, avgdl
        self._index: dict[str, dict] = {}
        for variety, items in self._by_variety.items():
            tokenized = [_tokenize(it.original) for it in items]
            doclens = [len(t) for t in tokenized]
            avgdl = sum(doclens) / len(doclens) if doclens else 0.0
            # doc frequency
            df: dict[str, int] = {}
            for toks in tokenized:
                for t in set(toks):
                    df[t] = df.get(t, 0) + 1
            n_docs = len(tokenized)
            idf = {t: math.log(1 + (n_docs - c + 0.5) / (c + 0.5)) for t, c in df.items()}
            self._index[variety] = {
                "tokenized": tokenized,
                "doclens": doclens,
                "avgdl": avgdl,
                "idf": idf,
                "items": items,
            }

    def retrieve(
        self,
        query: str,
        variety: str,
        top_k: int = 3,
    ) -> list[CorpusEntry]:
        """Return top_k entries in `variety` ranked by BM25 over query.

        Returns up to top_k highest-scoring entries; if variety has fewer
        examples than top_k, returns all of them. Returns [] if variety not
        in corpus or corpus empty.
        """
        idx = self._index.get(variety)
        if not idx:
            return []
        q_toks = _tokenize(query)
        if not q_toks:
            return idx["items"][:top_k]
        scores: list[tuple[float, int]] = []
        avgdl = idx["avgdl"] or 1.0
        for i, toks in enumerate(idx["tokenized"]):
            score = 0.0
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            dl = idx["doclens"][i] or 1
            for t in q_toks:
                if t not in tf:
                    continue
                idf_t = idx["idf"].get(t, 0.0)
                if idf_t <= 0:
                    continue
                f = tf[t]
                num = f * (self.K1 + 1)
                den = f + self.K1 * (1 - self.B + self.B * dl / avgdl)
                score += idf_t * num / den
            scores.append((score, i))
        scores.sort(key=lambda x: x[0], reverse=True)
        # If top score is 0, fall back to first top_k entries (better than nothing)
        if scores and scores[0][0] <= 0:
            return idx["items"][:top_k]
        return [idx["items"][i] for _, i in scores[:top_k]]

    def variety_size(self, variety: str) -> int:
        idx = self._index.get(variety)
        return len(idx["items"]) if idx else 0


def format_examples_for_prompt(entries: list[CorpusEntry]) -> list[str]:
    """Render retrieved examples as prompt-friendly lines."""
    lines: list[str] = []
    for e in entries:
        lines.append(f"  · 原文「{e.original}」 → 本地人会说「{e.rewrite}」")
    return lines


# ---------- module singleton ----------

_default_lock = threading.Lock()
_default_retriever: BM25Retriever | None = None


def get_default_retriever() -> BM25Retriever | None:
    """Lazy-load module-level retriever from default corpus path.

    Returns None if NCGA_CORPUS_DISABLE=1 OR corpus file missing OR empty.
    Safe to call from any thread.
    """
    global _default_retriever
    if os.environ.get("NCGA_CORPUS_DISABLE") == "1":
        return None
    if _default_retriever is not None:
        return _default_retriever
    with _default_lock:
        if _default_retriever is not None:  # double-check after lock
            return _default_retriever
        path_override = os.environ.get("NCGA_CORPUS_PATH")
        entries = load_corpus(path_override or _DEFAULT_CORPUS_PATH)
        # 2026-08 quality fix: only verified entries may be injected as
        # 【本地人示例】 ground truth. needs_review rows are unreviewed LLM
        # drafts — serving them as few-shot taught the model their mistakes
        # (36% of the corpus, incl. ALL 40 putonghua + 24 guangdong entries,
        # was being shown to the model as "真实的本地说法").
        entries = [e for e in entries if e.quality_tier == "verified"]
        if not entries:
            return None
        _default_retriever = BM25Retriever(entries)
        return _default_retriever


def _reset_default_retriever_for_tests() -> None:
    """Test hook to drop the cached singleton (e.g. between corpus changes)."""
    global _default_retriever
    with _default_lock:
        _default_retriever = None
