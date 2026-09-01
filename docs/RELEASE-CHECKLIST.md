# NCGA 发布就绪检查 / Release Readiness

> Finalized by Kimi Code · 2026-05-31 · branch `main`

## ✅ Verified (I ran these)

| Area | Result |
|---|---|
| **Secrets / git hygiene** | `.env` gitignored + untracked; auth token & API key: **0 matches in tracked files AND full git history**; `.env.example` all placeholders. Safe to publish. |
| **Unit tests** | `pytest tests/test_app.py` → all pass (offline, LLM mocked) |
| **Lint / format** | `ruff check .` + `ruff format --check .` → clean |
| **Real-LLM smoke** | `tools/smoke.sh` → **14/14 PASS** (GETs, auth-gating 401, path-traversal 404, 3 live DeepSeek POSTs, 3 validation 400s) |
| **Data** | corpus 400 (40/variety, 390 verified / 10 held), lexicon 1189 (sourced, 0 malformed) |
| **Loose ends** | 0 TODO/FIXME/console.log/debugger in shipping code |
| **Repo distribution** | LICENSE present; README quickstart present; default branch serves the work |
| **Git state** | working tree clean, 0 unpushed |

## ⚠️ UNVERIFIED / known limits (honest ledger)

| Item | Status |
|---|---|
| **Chrome extension end-to-end** | Code-complete, all known bugs fixed, syntax-clean — but NOT confirmed in a live browser (especially 即时 mode). Needs a manual Chrome pass per `extension/INSTALL.md`. |
| **10 Minnan (福建闽南) corpus rows** | Held as `needs_review` — Fujian-specific authenticity can't be verified from data alone; needs a native speaker. The other 390 rows are verified. |
| **Global discipline hook** | Installed at `~/.kimi-code/`; whether it fires live needs a `/hooks` reload to confirm (separate from this repo). |

## How others run it (after cloning)

```bash
git clone <repo-url>
cd ncga
pip install -r requirements.txt
cp .env.example .env          # then put a DeepSeek API key in DEEPSEEK_API_KEY=
python app.py                 # http://127.0.0.1:8000
```

No key? It still runs — falls back to an offline heuristic rewriter and says so.

## What's safe to share

- **The repo (code + data)** — yes, verified secret-clean. This is the easy path.
- **A live URL** — would need a TLS reverse proxy + your own API key paying for all traffic + abuse limits. Not set up.
- **The Chrome extension** — only after the live Chrome pass above.

## 方言质量门槛 (2026-08 起生效)

任何改动 prompt / 模型 / 路由 / 语料的提交前:

```bash
python3 tools/eval_dialects.py            # 任一方言较基线掉 >0.3 → exit 1,阻断提交
python3 tools/eval_dialects.py --limit 3  # 省钱快扫(每方言 3 行)
```

基线:`data/eval_baseline.json`(由 `--set-baseline` 写入);每次运行落 `data/eval_runs/` 并在 `/quality` 看板展示趋势。评审:`deepseek-v4-pro`,temperature=0,双评取中位数,prompt 版本 `anchored-v1`。详见 README「方言质量评估协议」。
