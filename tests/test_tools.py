"""Tests for tools/ scripts: build_corpus.py and review_corpus.py.

Loaded via importlib (same pattern as ImporterValidationTests in test_app.py)
because tools/ is not a package.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / f"tools/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BuildCorpusContractTests(unittest.TestCase):
    """Regression guard for the api-contract bug: build_corpus once called
    general_chat(system_prompt=..., user_prompt=...) — kwargs that do not
    exist on the real method — and only failed at runtime, per request,
    inside a broad try/except."""

    def test_general_chat_kwargs_bind_to_real_signature(self) -> None:
        mod = _load_tool("build_corpus")
        from native_chinese_assistant.rewrite import ChatCompletionsClient

        sig = inspect.signature(ChatCompletionsClient.general_chat)
        kwargs = mod.chat_request_kwargs("测试提示词")
        try:
            bound = sig.bind(mock.Mock(), **kwargs)  # Mock stands in for self
        except TypeError as exc:
            self.fail(f"build_corpus kwargs no longer match general_chat signature: {exc}")
        messages = bound.arguments["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        self.assertIn("测试提示词", messages[1]["content"])
        self.assertEqual(bound.arguments["max_tokens"], 400)

    def test_scenarios_align_with_import_corpus_enum(self) -> None:
        bc = _load_tool("build_corpus")
        ic = _load_tool("import_corpus")
        self.assertEqual(set(bc.SCENARIOS), set(ic.VALID_SCENARIOS))


class ReviewCorpusDecisionTests(unittest.TestCase):
    """The reproduced failure: the old loop rewrote raw.jsonl after every
    decision while iterating positional indices, so indices went stale —
    rows were silently skipped or raised IndexError. The rewrite collects
    decisions by (variety, original) key over a snapshot and writes once."""

    ROWS = [
        {
            "variety": "beijing_mandarin",
            "scenario": "greeting",
            "original": "早上好,吃了吗",
            "rewrite": "早儿啊您内,吃了吗您",
            "quality_tier": "needs_review",
            "notes": "",
        },
        {
            "variety": "shanghai_mandarin_style",
            "scenario": "complaint",
            "original": "今天地铁太挤了",
            "rewrite": "今朝地铁挤得来",
            "quality_tier": "needs_review",
            "notes": "",
        },
        {
            "variety": "cantonese_written",
            "scenario": "praise",
            "original": "你做得很好",
            "rewrite": "你做得好掂呀",
            "quality_tier": "needs_review",
            "notes": "",
        },
    ]

    def setUp(self) -> None:
        self.mod = _load_tool("review_corpus")
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.mod.RAW_PATH = base / "corpus_raw.jsonl"
        self.mod.APPROVED_PATH = base / "corpus.jsonl"
        self.mod.REJECTED_PATH = base / "corpus_rejected.jsonl"
        self.mod.CURSOR_PATH = base / "corpus_review_cursor.txt"
        with self.mod.RAW_PATH.open("w", encoding="utf-8") as f:
            for r in self.ROWS:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_three_rows_all_decided_no_skip_no_indexerror(self) -> None:
        # y / n / y — exactly 3 prompts; a stale-index loop would re-read the
        # shrinking file and skip row 2 or 3 (or raise IndexError).
        with mock.patch("builtins.input", side_effect=["y", "n", "y"]):
            rc = self.mod.main([])
        self.assertEqual(rc, 0)

        approved = self.mod.load_jsonl(self.mod.APPROVED_PATH)
        rejected = self.mod.load_jsonl(self.mod.REJECTED_PATH)
        remaining = self.mod.load_jsonl(self.mod.RAW_PATH)
        self.assertEqual(len(approved), 2)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(remaining, [])
        self.assertEqual(
            {a["original"] for a in approved},
            {"早上好,吃了吗", "你做得很好"},
        )
        self.assertEqual(rejected[0]["original"], "今天地铁太挤了")
        for a in approved:
            self.assertEqual(a["quality_tier"], "verified")
        # completed run clears the cursor
        self.assertFalse(self.mod.CURSOR_PATH.is_file())

    def test_apply_decisions_is_pure_content_key_logic(self) -> None:
        decisions = {
            ("beijing_mandarin", "早上好,吃了吗"): ("approve", dict(self.ROWS[0], quality_tier="verified")),
            ("cantonese_written", "你做得很好"): ("reject", self.ROWS[2]),
        }
        remaining, approved, rejected = self.mod.apply_decisions(list(self.ROWS), decisions)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["original"], "今天地铁太挤了")
        self.assertEqual(len(approved), 1)
        self.assertEqual(len(rejected), 1)

    def test_invalid_scenario_not_promoted(self) -> None:
        bad_row = {
            "variety": "beijing_mandarin",
            "scenario": "not_a_real_scenario",
            "original": "随便说一句",
            "rewrite": "随便说一句呗",
            "quality_tier": "needs_review",
            "notes": "",
        }
        with self.mod.RAW_PATH.open("w", encoding="utf-8") as f:
            f.write(json.dumps(bad_row, ensure_ascii=False) + "\n")
        with mock.patch("builtins.input", side_effect=["y"]):
            rc = self.mod.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(self.mod.load_jsonl(self.mod.APPROVED_PATH), [])
        # invalid entry stays in raw for fixing
        self.assertEqual(len(self.mod.load_jsonl(self.mod.RAW_PATH)), 1)


if __name__ == "__main__":
    unittest.main()
