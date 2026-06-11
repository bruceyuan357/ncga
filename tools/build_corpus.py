#!/usr/bin/env python3
"""Expand data/corpus.jsonl to 30 examples per variety via DeepSeek.

Usage:
    python3 tools/build_corpus.py              # extends to 30 per variety
    python3 tools/build_corpus.py --target 50  # extends to 50 per variety
    python3 tools/build_corpus.py --varieties shanghai_mandarin_style,cantonese_written
    python3 tools/build_corpus.py --dry-run    # show prompts, don't call LLM

Output goes to data/corpus_raw.jsonl (separate from approved corpus.jsonl).
After generating, run `python3 tools/review_corpus.py` to thumb up/down and
promote approved entries into data/corpus.jsonl.

Requires DEEPSEEK_API_KEY in environment or .env.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from native_chinese_assistant.corpus import load_corpus  # noqa: E402
from native_chinese_assistant.presets import PRESET_METADATA, VarietyPreset  # noqa: E402
from native_chinese_assistant.rewrite import (  # noqa: E402
    ChatCompletionsClient,
    load_llm_config,
)

# Scenarios we want covered — must stay aligned with VALID_SCENARIOS in
# tools/import_corpus.py (the canonical 10-value enum data/corpus.jsonl uses).
SCENARIOS = [
    "complaint",
    "request",
    "praise",
    "greeting",
    "small_talk",
    "self_depreciate",
    "teach_kid",
    "comfort",
    "invite",
    "apology",
]

SYSTEM_PROMPT = "你是中文方言语料生成助手。严格按要求输出 JSON,无 markdown,无解释。"


def chat_request_kwargs(prompt: str) -> dict:
    """Kwargs for ChatCompletionsClient.general_chat.

    Kept in one place so tests can bind them against the real
    inspect.signature of general_chat (api-contract regression guard).
    """
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 400,
    }

# Topic seeds to vary the "original" text so we don't get 30 nearly-identical samples
TOPIC_SEEDS = [
    "天气 / 出行 / 早晨上班",
    "吃饭 / 点菜 / 推荐餐厅",
    "工作 / 加班 / 同事关系",
    "家庭 / 父母 / 周末安排",
    "购物 / 价格 / 还价",
    "孩子教育 / 学校 / 作业",
    "看病 / 健康 / 锻炼",
    "出差 / 机场 / 高铁",
    "节假日 / 春节 / 回家",
    "网络 / 手机 / 软件",
]


def build_generation_prompt(variety_key: str, scenario: str, topic_seed: str) -> str:
    metadata = PRESET_METADATA[VarietyPreset(variety_key)]
    return f"""你帮我生成一条方言重写训练数据。要求:

【方言】{metadata.label}
【场景】{scenario}(短情境标签,不要解释)
【话题方向】{topic_seed}

任务:
1. 先想一句普通话原文(20-40 字,有具体动作/对象,不要空泛感叹)
2. 然后给出该方言的本地化改写(允许加方言专有词、语气词、语序变化、文化梗)
3. 改写必须像本地人随口说的话,不要新闻腔/教科书腔

严格 JSON 输出,无 markdown,无前导语:
{{"original": "普通话原文", "rewrite": "{metadata.label}改写", "notes": "本条最关键的 1-2 个方言点"}}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=30, help="examples per variety to reach")
    ap.add_argument(
        "--varieties",
        type=str,
        default="",
        help="comma-separated subset (default: all 10)",
    )
    ap.add_argument("--dry-run", action="store_true", help="print prompts only, no LLM call")
    ap.add_argument(
        "--out",
        type=str,
        default=str(REPO_ROOT / "data" / "corpus_raw.jsonl"),
        help="output JSONL (append mode)",
    )
    args = ap.parse_args()

    config = None
    if not args.dry_run:
        config = load_llm_config()  # reads .env itself; None means no API key
        if config is None:
            print(
                "ERROR: no API key — set DEEPSEEK_API_KEY or LLM_API_KEY (in env or .env)",
                file=sys.stderr,
            )
            return 2

    existing = load_corpus()
    have_counts: dict[str, int] = {}
    for e in existing:
        have_counts[e.variety] = have_counts.get(e.variety, 0) + 1

    selected = (
        [v.strip() for v in args.varieties.split(",") if v.strip()]
        if args.varieties
        else [v.value for v in VarietyPreset]
    )

    print(f"Target {args.target} per variety. Currently:")
    for v in selected:
        print(f"  {v}: have {have_counts.get(v, 0)} → need {max(0, args.target - have_counts.get(v, 0))}")
    print()

    client = None if args.dry_run else ChatCompletionsClient(config)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    total_generated = 0
    total_needed = 0
    for variety_key in selected:
        need = max(0, args.target - have_counts.get(variety_key, 0))
        total_needed += need
        if need == 0:
            continue
        print(f"\n=== {variety_key}: generating {need} ===")
        for i in range(need):
            scenario = SCENARIOS[(have_counts.get(variety_key, 0) + i) % len(SCENARIOS)]
            seed = random.choice(TOPIC_SEEDS)
            prompt = build_generation_prompt(variety_key, scenario, seed)
            if args.dry_run:
                print(f"  [{i + 1}/{need}] scenario={scenario}, seed={seed}")
                continue
            try:
                # general_chat returns the raw content string from the first choice
                resp = client.general_chat(**chat_request_kwargs(prompt))
                obj = json.loads(resp)
                if not isinstance(obj, dict):
                    print(f"  [{i + 1}/{need}] BAD response (not a JSON object), skipping")
                    continue
                original = str(obj.get("original", "")).strip()
                rewrite = str(obj.get("rewrite", "")).strip()
                notes = str(obj.get("notes", "")).strip()
                if not original or not rewrite:
                    print(f"  [{i + 1}/{need}] BAD response (empty fields), skipping")
                    continue
                line = json.dumps(
                    {
                        "variety": variety_key,
                        "scenario": scenario,
                        "original": original,
                        "rewrite": rewrite,
                        "quality_tier": "needs_review",
                        "notes": notes,
                    },
                    ensure_ascii=False,
                )
                with out_path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                total_generated += 1
                print(f"  [{i + 1}/{need}] OK · {original[:25]}...")
                # gentle on API
                time.sleep(0.3)
            except Exception as e:
                print(f"  [{i + 1}/{need}] FAIL: {e}")
    if not args.dry_run and total_needed > 0 and total_generated == 0:
        print("\nERROR: 0 entries generated (every attempt failed) — see FAIL lines above", file=sys.stderr)
        return 1
    print(f"\n✓ generated {total_generated} new entries → {out_path}")
    print("Next: python3 tools/review_corpus.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
