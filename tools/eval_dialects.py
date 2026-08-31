#!/usr/bin/env python3
"""Dialect eval harness (Stages 2+3 of the eval protocol).

Runs the golden eval split (data/eval_golden.jsonl) through the production
rewrite pipeline and scores each output with a REFERENCE-ANCHORED judge:
the judge sees original + native reference + candidate and grades meaning
preservation and dialect fidelity against the reference — far more stable
than the free-floating "nativeness" rubric used by tools/quality_sweep.py.

Reliability rules:
  - judge pinned: deepseek-v4-pro, temperature=0, prompt version recorded
  - every cell judged twice, median taken
  - deltas vs baseline smaller than NOISE_FLOOR are treated as noise
  - self-test: judge the reference against itself (must be >= 4.5) before
    spending money on the full run

Usage:
  python3 tools/eval_dialects.py                 # full run, compare vs baseline
  python3 tools/eval_dialects.py --set-baseline  # write data/eval_baseline.json
  python3 tools/eval_dialects.py --varieties jianghuai_or_lower_yangtze_mandarin
  python3 tools/eval_dialects.py --limit 3       # first N rows per variety (cheap)

Exit code 1 when any variety drops more than NOISE_FLOOR below baseline —
wire this into your release checklist.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from native_chinese_assistant.corpus import load_eval_entries  # noqa: E402
from native_chinese_assistant.presets import VarietyPreset  # noqa: E402
from native_chinese_assistant.rewrite import (  # noqa: E402
    ChatCompletionsClient,
    RewriteError,
    RewriteService,
    load_dotenv,
    load_llm_config,
)

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "data" / "eval_runs"
BASELINE_PATH = ROOT / "data" / "eval_baseline.json"

JUDGE_MODEL = "deepseek-v4-pro"
JUDGE_VERSION = "anchored-v1"
NOISE_FLOOR = 0.3  # per-variety mean delta smaller than this is judge noise
SELF_TEST_MIN = 4.5  # judge(reference vs reference) must clear this

_ANCHORED_JUDGE_PROMPT = (
    "你是方言质量评审。给你【原文】、【参考改写】(母语者标准答案)、【候选改写】。\n"
    "只评价【候选改写】，按两个维度对照：\n"
    "1. 意思保留：与【原文】意思一致，不添不减；\n"
    "2. 方言地道度：与【参考改写】的方言风格、用词、句式在同一水准。\n\n"
    "打分 0-5：5=和参考一样地道且意思完全一致；3=意思对但方言味明显弱于参考；"
    "1=基本是普通话直译；0=意思错误或完全不像方言。\n"
    "给一句 ≤20 字理由，指出与参考的最大差距。\n"
    '严格 JSON：{"score": 0-5, "reason": "..."}，不要 markdown。'
)


def _judge_one(client: ChatCompletionsClient, original: str, reference: str, candidate: str) -> float:
    """One anchored judgment, temperature 0. Median-of-2 happens upstream."""
    user = json.dumps(
        {"原文": original, "参考改写": reference, "候选改写": candidate},
        ensure_ascii=False,
    )
    for attempt in range(3):
        content = client.general_chat(
            [
                {"role": "system", "content": _ANCHORED_JUDGE_PROMPT},
                {"role": "user", "content": user},
            ],
            max_tokens=800,
            temperature=0.0,
            thinking="low",
            model=JUDGE_MODEL,
        )
        if content and content.strip():
            from native_chinese_assistant.rewrite import _parse_llm_json

            parsed = _parse_llm_json(content)
            return max(0.0, min(5.0, float(parsed.get("score", 0))))
        time.sleep(1 + attempt)
    raise RewriteError("judge returned empty content after 3 attempts")


def judge_median(client: ChatCompletionsClient, original: str, reference: str, candidate: str) -> float:
    """Two independent judgments, median — halves single-call judge variance."""
    scores = [
        _judge_one(client, original, reference, candidate),
        _judge_one(client, original, reference, candidate),
    ]
    return statistics.median(scores)


def compare_to_baseline(current: dict[str, float], baseline: dict, floor: float = NOISE_FLOOR) -> list[str]:
    """Return per-variety regression messages (empty list = pass)."""
    failures = []
    base_varieties = baseline.get("varieties", {})
    for variety, base_mean in base_varieties.items():
        cur = current.get(variety)
        if cur is None:
            failures.append(f"{variety}: missing from current run (baseline {base_mean:.2f})")
        elif cur < base_mean - floor:
            failures.append(f"{variety}: {cur:.2f} < baseline {base_mean:.2f} - {floor}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--varieties", type=str, default="", help="comma-separated subset")
    parser.add_argument("--limit", type=int, default=0, help="first N rows per variety")
    parser.add_argument("--parallel", type=int, default=3)
    parser.add_argument("--set-baseline", action="store_true", help="write this run as the baseline")
    parser.add_argument("--dry-run", action="store_true", help="print scores; write no files")
    args = parser.parse_args()

    load_dotenv()
    config = load_llm_config()
    if config is None:
        print("error: no LLM configured", file=sys.stderr)
        return 2
    client = ChatCompletionsClient(config)
    service = RewriteService(client=client)

    rows = load_eval_entries()
    if not rows:
        print("error: no golden eval rows — run tools/build_eval_split.py first", file=sys.stderr)
        return 2
    if args.varieties:
        wanted = {v.strip() for v in args.varieties.split(",") if v.strip()}
        rows = [r for r in rows if r["variety"] in wanted]
    if args.limit > 0:
        per_variety: dict[str, int] = {}
        limited = []
        for r in rows:
            n = per_variety.get(r["variety"], 0)
            if n < args.limit:
                limited.append(r)
                per_variety[r["variety"]] = n + 1
        rows = limited
    if not rows:
        print("error: selection is empty", file=sys.stderr)
        return 2

    # Judge self-test: the reference graded against itself must score high,
    # otherwise the judge config (model/prompt/API) is broken and every
    # number below would be garbage.
    print("judge self-test (reference vs itself)…", end=" ", flush=True)
    probe = rows[0]
    try:
        st = judge_median(client, probe["original"], probe["reference_rewrite"], probe["reference_rewrite"])
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED to call judge: {exc}", file=sys.stderr)
        return 2
    print(f"{st:.1f}/5")
    if st < SELF_TEST_MIN:
        print(f"error: judge self-test {st:.1f} < {SELF_TEST_MIN} — judge is unreliable, aborting", file=sys.stderr)
        return 2

    git_sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT
    ).stdout.strip()

    print(f"eval: {len(rows)} golden rows × (1 rewrite + 2 anchored judgments)")

    def run_cell(row: dict) -> tuple[str, float | None]:
        variety = VarietyPreset(row["variety"])
        try:
            result = service.rewrite(row["original"], variety)
            if result.degraded:
                print(f"  warn: degraded (heuristic) for {row['variety']} | {row['original'][:12]}…")
                return row["variety"], 0.0  # degraded output is a real quality zero
            score = judge_median(client, row["original"], row["reference_rewrite"], result.rewritten_text)
            return row["variety"], score
        except Exception as exc:  # noqa: BLE001
            print(f"  warn: cell failed for {row['variety']} | {row['original'][:12]}…: {exc}")
            return row["variety"], None

    by_variety: dict[str, list[float]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        for variety, score in pool.map(run_cell, rows):
            done += 1
            if score is not None:
                by_variety.setdefault(variety, []).append(score)
            if done % 20 == 0:
                print(f"  …{done}/{len(rows)}")

    means: dict[str, float] = {}
    print(f"\n{'variety':<42} {'n':>4} {'mean':>6} {'min':>5} {'max':>5}")
    for variety in sorted(by_variety):
        vals = by_variety[variety]
        mean = statistics.fmean(vals)
        means[variety] = mean
        print(f"{variety:<42} {len(vals):>4} {mean:>6.2f} {min(vals):>5.1f} {max(vals):>5.1f}")
    overall = statistics.fmean([m for m in means.values()]) if means else 0.0
    print(f"\noverall mean: {overall:.2f}")

    report = {
        "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git_sha": git_sha,
        "judge": {"model": JUDGE_MODEL, "prompt_version": JUDGE_VERSION, "temperature": 0.0, "reps": 2},
        "rows": len(rows),
        "overall": round(overall, 3),
        "varieties": {v: round(m, 3) for v, m in means.items()},
    }

    if args.set_baseline:
        BASELINE_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"baseline written → {BASELINE_PATH}")
    if not args.dry_run:
        RUNS_DIR.mkdir(exist_ok=True)
        run_path = RUNS_DIR / (time.strftime("%Y-%m-%dT%H%M") + ".json")
        run_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"run recorded → {run_path.relative_to(ROOT)}")

    if BASELINE_PATH.is_file() and not args.set_baseline:
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        failures = compare_to_baseline(means, baseline)
        if failures:
            print("\nREGRESSION vs baseline:")
            for f in failures:
                print(f"  ✗ {f}")
            return 1
        print("\nno regression vs baseline (floor 0.3)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
