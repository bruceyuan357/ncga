# NCGA 全功能审计 + Opus 4.7 工作总结

> **审计人**: Opus 4.8 (1M)
> **审计日期**: 2026-05-19
> **被审范围**: `cfa02c8` (baseline) → `5227201` (Stage D),共 **54 个 commit**,全部由 Opus 4.7 完成
> **方法**: 不只读代码 — 跑测试、跑 lint、启服务器、curl 每个 endpoint、node 跑 crypto roundtrip、importer 去重往返
> **结论**: **系统站得住。** 核心改写质量优秀,架构干净。审计发现 1 个真实测试回归 + 1 处未提交数据风险,均已当场闭合。

---

## 1. 一句话总览

Opus 4.7 在 baseline 之后做了 5 个大阶段的工作,把 NCGA 从"能改写的后端"推进到"有完整 v3 视觉 + Chrome 扩展 + 语料库增强 + 词典增强"的完整产品形态。**198 tests pass,ruff 双绿,核心 /api/rewrite 产出地道方言**(实测 `你今天看起来很漂亮` → `侬今朝看起来老嗲额!`)。

---

## 2. Opus 4.7 干了什么(按阶段)

| 阶段 | commits | 内容 | 验证状态 |
|---|---|---|---|
| **Cycle 20** (后端) | 6 (`56ff2b1`→`fb73635`) | 多语言输入 · `/api/feedback` + JSONL · 双轨认证(HMAC cookie + Bearer)· `/api/logout` cookie 撤销 · CSP 去 unsafe-inline | ✅ PASS |
| **Cycle 21** (自审) | 9 (`8b5fbdc`→`96ec245`) | quality-stats 限流 · characterize 去控制字符 · UnicodeDecodeError 包装 · QualityStore RLock 死锁修复 · `_parse_llm_json` · batch 先验证后扣额 · lru_cache 3 个 prompt builder | ✅ PASS |
| **Stage A** (v3 视觉一版) | 8 (`4f1d224`→`340d396`) | Manrope 字体 · 四季调色板 · 40vh hero · sidebar 抽屉 · 杂志卡 · 16 古诗 · 40 季节地标 | ✅ PASS |
| **v3 R-A** (视觉重做) | 4 (`bcead2a`→`89fd4d5`) | hero 排版舒展 · 调色板提亮(深粉→樱粉)· 樱花瓣飘落 · 节气小章 | ✅ PASS |
| **Stage B** (Chrome 扩展) | ~13 (`fae1847`→`125b8d0`) | MV3 manifest · service worker · AES-GCM token 加密 · popup · options · Shadow DOM overlay · 右键菜单 · 选区自动改写 popover · 安装文档 | ✅ PASS (代码 + crypto + 语法全验) |
| **v3 M** (迁移为默认) | ~9 (`2267f63`→`45aedd2`) | v3 设为默认 · 全站浅色 · daily v3 化 · 全站樱花瓣 · 拉宽 1280 · 主题切换器修复 | ✅ PASS |
| **v3 C** (语料库) | 3 (`24e5095`,`51f35ef`,`61cf934`) | 100 条手写语料 · 纯 stdlib BM25 检索 · system prompt 注入 · 审批工具 | ✅ PASS |
| **Stage D** (词典/Cowork) | 1 (`5227201`) | lexicon 模块 · corpus+lexicon 双 importer · URL 验收脚本 · 10 测试 | ✅ PASS (数据本轮补提交) |

---

## 3. 关键功能逐项验证

### 3.1 后端(live,真启服务器 + curl)

| 功能 | 方法 | 结果 |
|---|---|---|
| 服务器启动 + healthz | `python3 app.py` + `curl /api/healthz` | ✅ `{"status":"ok"}` |
| `/api/presets` | curl | ✅ 10 presets |
| **认证 gating** | 无 token POST `/api/rewrite` | ✅ `401` |
| **`/api/rewrite`** (核心) | 带 token POST | ✅ `侬今朝看起来老嗲额!`(degraded=false) |
| **corpus+lexicon 注入** | 改写 `漂亮` → 输出含 `嗲` | ✅ 词典条目 `漂亮→嗲` 实际流入输出 |
| `/api/phrase-of-the-day` | curl | ✅ `200`(注:真实路径是 `phrase-of-the-day`,非 `daily-phrase`) |
| `/api/quality-stats` | curl | ✅ `200` |
| importer 去重 | 自身往返 import | ✅ corpus 400→0 new,lexicon 1189→0 new |

### 3.2 数据资产

| | 条数 | 分布 | 质量 |
|---|---|---|---|
| **corpus.jsonl** | 400 | 每方言 40,10 场景均分 | 370 verified + 30 needs_review;300 带 source |
| **lexicon.jsonl** | 1189 | 每方言 ~119,6 类各 ~200 | 全部带 source,0 malformed |

> needs_review 的 30 条 = 粤语书面 / 台湾闽南语 / 福建闽南语(母语者欢迎校对)。

### 3.3 Chrome 扩展(代码 + 运行时验证)

| 功能 | 方法 | 结果 |
|---|---|---|
| manifest 合法性 | JSON parse | ✅ MV3,permissions 含 activeTab,无遗留 default_locale |
| 5 个 JS 模块语法 | `node --check` ×5 | ✅ 全过 |
| **AES-GCM token 加密** | node roundtrip | ✅ encrypt→decrypt 还原 |
| **错密码拒绝** | 用错 passphrase 解密 | ✅ 正确抛错(认证完整性) |
| SW 字段名修复 | grep `rewritten_text` | ✅ 在(曾因读错字段导致"无输出") |
| 动态注入 fallback | grep `executeScript` | ✅ 在(曾因旧 tab 没注入导致浮窗不来) |
| content 防重注入 | grep `__ncga_content_loaded__` | ✅ 在 |
| overlay 双模式 | grep `overlayMode` | ✅ corner / anchor 区分(修一闪而过 bug) |
| 樱花 icon | 文件存在 | ✅ 16/32/48/128 PNG |

### 3.4 自动化测试 / lint

| 检查 | 结果 |
|---|---|
| `pytest tests/test_app.py` | ✅ **198 passed**(修复后) |
| `ruff check .` | ✅ All checks passed |
| `ruff format --check` | ✅ 全格式化(修复后) |
| 浏览器 E2E(playwright) | ⚠️ 4 个 error — **仅缺 `playwright install chromium`,非代码 bug** |

---

## 4. 审计发现的问题 + 处置

| # | 严重度 | 问题 | 根因 | 处置 |
|---|---|---|---|---|
| 1 | **HIGH** | corpus.jsonl(+300)+ lexicon.jsonl(+1189)**未提交**,在 worktree 裸奔 | Stage D 工具 import 了 Cowork 数据但没 commit | ✅ **已提交** `63b2beb` — 这正是之前 14h 丢失灾难的同类 Rule #1 风险 |
| 2 | **MED** | `test_corpus_jsonl_loads_and_has_100_entries` FAIL(全套唯一红) | 硬编码 `== 100`,Stage D 扩到 400 后失效 | ✅ **已修** `8edc5eb` — 改为结构不变式(≥100 + 10 方言 + 均衡) |
| 3 | **LOW** | 4 个 Stage D tools 脚本 + 3 模块未 ruff format | Stage D commit 时漏跑 format,CI 会红 | ✅ **已修** `8edc5eb` — 纯格式化无逻辑改动 |
| 4 | **INFO** | 浏览器 E2E 4 个 error | 环境缺 chromium 二进制 | 📋 跑 `playwright install chromium` 即可,非代码问题 |
| 5 | **INFO** | Cowork 数据低于 prompt 目标(corpus 400 vs 500,lexicon 1189 vs 2000) | Cowork 交付量 / importer 去重 | 📋 数据本身均衡干净,不阻塞;后续可再 import 补足 |

**净结论**: 4.7 的工作**没有真实代码缺陷**。3 个可闭合项全部当场闭合,2 个 INFO 项是环境/数据量,非 bug。

---

## 5. 架构评价(4.8 视角)

**优点**:
- **零运行时依赖原则贯彻到底** — corpus/lexicon 都用纯 stdlib BM25(不引 torch/sentence-transformers),`lexicon.py` 几乎完美镜像 `corpus.py`(同 BM25、同 singleton、同 reset hook、同 env-disable)。
- **防御式注入** — `_build_payload` 里 corpus + lexicon 检索都包在 try/except,检索挂掉绝不连累改写主路径。
- **corpus(句子级 few-shot)与 lexicon(词级 hint)正交分工** 清晰,prompt 注入块措辞克制("能自然用上就用,不要强塞")。
- **扩展安全模型扎实** — token AES-GCM(PBKDF2-250k)落 `chrome.storage.session`,activeTab 而非 `<all_urls>` host 权限。

**可改进(非阻塞)**:
- corpus 100 条原手写无 source 字段;若将来要全量审计来源,需补。
- 浏览器 E2E 未纳入常规 CI(缺 chromium 安装步骤)。
- needs_review 的粤/闽数据仍待母语者校对。

---

## 6. 给 Bruce 的后续建议(按优先级)

1. **`git push`** — 本轮 2 个新 commit(数据 + 审计修复)还在本地,push 才闭合 Rule #1。
2. **`playwright install chromium`** — 让 4 个 E2E 测试能跑(可选,需要时再做)。
3. **真机重测扩展** — 代码全绿,但右键浮窗 / 选区 popover 这类要 Chrome 实测确认 UX(按 `extension/INSTALL.md` 走)。
4. **needs_review 数据校对** — 找粤/沪/闽母语者盲评,把 30 条 needs_review 升 verified。
5. **数据补足(可选)** — 若要达 prompt 原目标(500 corpus / 2000 lexicon),再跑一轮 Cowork + `tools/import_*.py`。

---

## 7. 硬数字

```
后端 Python:     5,498 行(8 模块)
测试:            198 passed, 0 failed
corpus:          400 条(10 方言 × 40)
lexicon:         1,189 条(10 方言 × ~119,6 类)
扩展:            5 JS 模块 + 4 icon,全语法通过 + crypto 验证
commit (4.7):    54 个(cfa02c8..5227201)
commit (4.8 审计):2 个(63b2beb 数据 + 8edc5eb 修复)
ruff:            check + format 双绿
```
