"""Dialect word-mapping lexicon + BM25 retriever (zero-dep).

Cycle 22 Stage D: dictionary-level "Mandarin word → variety word" mappings
sourced from authoritative references (汉典, Wiktionary, 中国语言资源保护工程, 等).
Used in conjunction with corpus.py: corpus provides sentence-level few-shot
examples; lexicon provides word-level "this is the local word for X" hints.

Both get injected into the system prompt at rewrite time, giving the LLM:
  1. (corpus) "here are 3 example sentences in this variety"
  2. (lexicon) "here are 5 dialect words you should consider using"

Schema (one JSONL entry per line):
  {
    "variety": "shanghai_mandarin_style",
    "mandarin": "漂亮",
    "local":    "嗲",
    "category": "idiom",     # particle/verb/noun/greeting/idiom/pronoun
    "ipa":      "tia44",     # optional, romanization/IPA
    "example_sentence": "侬今朝穿了哈嗲咯",  # optional
    "source":   "https://en.wiktionary.org/wiki/嗲",
    "notes":    ""
  }

Public API mirrors corpus.py:
    load_lexicon(path) -> list[LexiconEntry]
    LexiconRetriever(entries) -> .retrieve(query, variety, top_k=5)
    format_lexicon_for_prompt(entries) -> list[str]
    get_default_lexicon_retriever() -> module-level singleton
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# Reuse the corpus tokenizer (same CJK + ASCII strategy)
from native_chinese_assistant.corpus import _tokenize

# Path to the lexicon JSONL. Override via NCGA_LEXICON_PATH for tests.
_DEFAULT_LEXICON_PATH = Path(__file__).parent.parent / "data" / "lexicon.jsonl"

# Categories per the schema; entries with unknown category fall back to "other".
_VALID_CATEGORIES = frozenset({"particle", "verb", "noun", "greeting", "idiom", "pronoun", "other"})


@dataclass(frozen=True)
class LexiconEntry:
    """One Mandarin→variety word mapping."""

    variety: str
    mandarin: str
    local: str
    category: str = "other"
    ipa: str = ""
    example_sentence: str = ""
    source: str = ""
    notes: str = ""


def load_lexicon(path: str | Path | None = None) -> list[LexiconEntry]:
    """Load lexicon from JSONL. Returns empty list if file missing.

    Skips malformed lines silently — same policy as corpus loader.
    """
    p = Path(path) if path else _DEFAULT_LEXICON_PATH
    if not p.is_file():
        return []
    entries: list[LexiconEntry] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        try:
            obj = json.loads(s)
            cat = str(obj.get("category", "other")).lower()
            if cat not in _VALID_CATEGORIES:
                cat = "other"
            entries.append(
                LexiconEntry(
                    variety=str(obj["variety"]),
                    mandarin=str(obj["mandarin"]),
                    local=str(obj["local"]),
                    category=cat,
                    ipa=str(obj.get("ipa", "")),
                    example_sentence=str(obj.get("example_sentence", "")),
                    source=str(obj.get("source", "")),
                    notes=str(obj.get("notes", "")),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return entries


class LexiconRetriever:
    """Tiny BM25 over a fixed lexicon, indexed by variety.

    We index against the `mandarin` field (the user is rewriting Mandarin text,
    so retrieving by Mandarin word overlap is the right signal). For each hit
    the formatter shows mandarin→local mapping plus optional ipa/example.

    Usage:
        r = LexiconRetriever(load_lexicon())
        hits = r.retrieve("漂亮", variety="shanghai_mandarin_style", top_k=5)
    """

    K1 = 1.5
    B = 0.75

    def __init__(self, entries: Iterable[LexiconEntry]):
        self._by_variety: dict[str, list[LexiconEntry]] = {}
        for e in entries:
            self._by_variety.setdefault(e.variety, []).append(e)
        self._index: dict[str, dict] = {}
        for variety, items in self._by_variety.items():
            tokenized = [_tokenize(it.mandarin) for it in items]
            doclens = [len(t) or 1 for t in tokenized]
            avgdl = sum(doclens) / len(doclens) if doclens else 0.0
            df: dict[str, int] = {}
            for toks in tokenized:
                for t in set(toks):
                    df[t] = df.get(t, 0) + 1
            n = len(tokenized)
            idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
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
        top_k: int = 5,
    ) -> list[LexiconEntry]:
        """Return top_k lexicon entries in `variety` ranked by BM25 over query."""
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
        if scores and scores[0][0] <= 0:
            return idx["items"][:top_k]
        return [idx["items"][i] for _, i in scores[:top_k]]

    def variety_size(self, variety: str) -> int:
        idx = self._index.get(variety)
        return len(idx["items"]) if idx else 0


def format_lexicon_for_prompt(entries: list[LexiconEntry]) -> list[str]:
    """Render lexicon entries as prompt-friendly lines.

    Format: `  · 普通话「X」→ 本地说「Y」(类别;可选 IPA;可选例句)`
    """
    lines: list[str] = []
    for e in entries:
        tail_bits: list[str] = []
        if e.category and e.category != "other":
            tail_bits.append(e.category)
        if e.ipa:
            tail_bits.append(f"IPA: {e.ipa}")
        if e.example_sentence:
            tail_bits.append(f"例:{e.example_sentence}")
        tail = "(" + "; ".join(tail_bits) + ")" if tail_bits else ""
        lines.append(f"  · 普通话「{e.mandarin}」→ 本地说「{e.local}」{tail}")
    return lines


# ---------- module singleton ----------

_default_lock = threading.Lock()
_default_retriever: LexiconRetriever | None = None


def get_default_lexicon_retriever() -> LexiconRetriever | None:
    """Lazy-load module-level retriever from default lexicon path.

    Returns None if NCGA_LEXICON_DISABLE=1 OR file missing OR empty.
    """
    global _default_retriever
    if os.environ.get("NCGA_LEXICON_DISABLE") == "1":
        return None
    if _default_retriever is not None:
        return _default_retriever
    with _default_lock:
        if _default_retriever is not None:
            return _default_retriever
        path_override = os.environ.get("NCGA_LEXICON_PATH")
        entries = load_lexicon(path_override or _DEFAULT_LEXICON_PATH)
        if not entries:
            return None
        _default_retriever = LexiconRetriever(entries)
        return _default_retriever


def _reset_default_lexicon_retriever_for_tests() -> None:
    """Test hook to drop the cached singleton."""
    global _default_retriever
    with _default_lock:
        _default_retriever = None
