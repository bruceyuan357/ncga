# NCGA 全项目评审（双语）/ Full-Project Review (Bilingual)

> 日期 / Date: 2026-06-10
> 方法 / Method: 7 维度并行检查 + 对每个 critical/high 发现做对抗验证（试图推翻）。20 个 agent，140 万 token，382 次工具调用。
> 7-dimension parallel inspection with adversarial verification (each high/critical finding was attacked by an independent skeptic). 20 agents, 1.4M tokens, 382 tool calls.
> 范围 / Scope: branch `claude/bold-bell-4750bf` @ commit 914af24 + 本地未推送的改动。

---

## 0. 一句话结论 / TL;DR

**中文** — 项目工程素养扎实(并发、验证诊断、加密、原子写都做对了),但有 13 个经验证的严重问题集中在三块:(1) **认证可绕过** — 任何能打开首页的人都能拿到一个有效 cookie 调用所有花钱的接口;(2) **流式改写路径静默丢功能** — 词典、Reflexion 覆盖、降级标记在主路径上全部失效;(3) **数据/工具链一堆"看起来在工作其实没有"** — CI 从没跑过、节气显示常年「大寒」、广东普通话语料退化成粤语、`build_corpus.py`/`review_corpus.py` 实跑即崩。**好消息:上一轮的「0% URL 可达」是我的验证脚本缺 CA 证书包,不是 Cowork 编数据 —— 修完是 95% 可达,语料是真的。**

**English** — The engineering discipline is genuinely strong (correct concurrency, validation diagnostics, encryption, atomic writes), but 13 verified serious findings cluster in three areas: (1) **auth is bypassable** — anyone who can load the homepage gets a valid cookie that calls every LLM-spending endpoint; (2) **the streaming rewrite path silently drops features** — glossary, Reflexion overrides, and the degraded flag are all no-ops on the primary path; (3) **a cluster of "looks-working-but-isn't" data/tooling** — CI has never run, the solar-term chip shows 大寒 year-round, the guangdong_mandarin corpus collapsed into Cantonese, and `build_corpus.py`/`review_corpus.py` crash on real use. **Good news: last round's "0% URLs reachable" was a missing-CA-bundle bug in my own verify script, not hallucinated Cowork data — fixed, it's 95% reachable, the corpus is real.**

---

## 1. 评分卡 / Scorecard

| 维度 / Dimension | 分数 / Score | 确认的严重问题 / Confirmed serious | 一句话 / One-liner |
|---|---|---|---|
| backend-core | 5 / 10 | 1 high + 2 high + (1→medium) | 工程底子好,但认证可绕过 + 流式路径丢功能 / Solid base, but bypassable auth + lossy streaming path |
| security | 8 / 10 | 0 | 成熟,自审痕迹明显;只剩中低风险 / Mature; only medium/low gaps left |
| frontend | 6 / 10 | 1 high | 节气常年显示「大寒」+ 大量死 CSS / Solar term wrong 11 months/yr + dead CSS |
| chrome-extension | 7 / 10 | 1 high | 设置页保存会清空弹窗设置 / Options save wipes popup settings |
| data-quality | 6.5 / 10 | 2 high | URL 是真的(95%);广东普通话退化成粤语 / URLs real (95%); guangdong collapsed to Cantonese |
| tests-tooling | 6 / 10 | 3 high | CI 从没跑过;两个工具实跑即崩 / CI never ran; two tools crash on real use |
| docs-process | 6.5 / 10 | 2 high | 文档修正卡在未推送的 main;Docker 不带语料 / Doc fixes stranded; Docker ships no corpus |

总计 / Total: **13 个确认严重问题**(对抗验证后,部分降级)。安全维度最强;前端 + 工具链最需要补。
**13 confirmed serious findings** (after adversarial down-grading). Security strongest; frontend + tooling need the most work.

---

## 2. 上一轮悬案已破 / The Open Question, Resolved

**「0/30 URL 全部不可达」到底是数据假还是脚本错?** — 脚本错。

- 根因 / Root cause: `tools/verify_corpus_quality.py` 用 `ssl.create_default_context()` **没带 CA 证书包**。在你这台 python.org 3.14 安装上(没跑过 `Install Certificates.command`),它谁都不信任,所以每个 HTTPS 都报 `CERTIFICATE_VERIFY_FAILED`。
- 证据 / Evidence: 同样的 8 个 URL,curl 全部 200;urllib 加上 `certifi.where()` 后也全部 200;同样的 User-Agent 也 200 —— 既不是反爬,也不是你的代理干扰。
- 已修 / Fixed (this review): 脚本改用 `certifi`,重跑 = **95% reachable**(corpus 19/20, lexicon 19/20)。唯一的"坏"是 `sutian.moe.edu.tw`(台湾教育部,TWCA 证书链不在 certifi 里,URL 真实)。
- 结论 / Verdict: **Cowork 的 400 条语料 + 1189 条词表,来源是真的,不要回滚。**

---

## 3. 确认的严重问题（按修复优先级）/ Confirmed Serious Findings (by fix priority)

> 每条都经过一个独立 agent 试图推翻后仍然成立。括号里是对抗验证后的最终严重度。
> Each survived an independent refutation attempt. Bracketed = severity after adversarial review.

### 🔴 P1 — 认证可绕过 / Bearer auth is bypassable (backend, high)
- **文件 / File**: `native_chinese_assistant/web.py:304-310`
- **中文**: `GET /` 不需要认证,但每次返回首页都无条件签发一个有效的 HMAC `ncga_sess` cookie;`_check_auth` 把这个 cookie 当作完整凭证接受所有 `POST /api/*`。所以任何人 `curl http://host/` 读到 Set-Cookie,就能调用所有花钱的 LLM 接口。Bearer token「手动发放」的设计目的被两个请求击穿。唯一剩下的防线是限流。
- **English**: `GET /` is unauthenticated yet unconditionally mints a valid HMAC cookie on every index serve; `_check_auth` accepts it as a full credential for all `POST /api/*`. Anyone can `curl http://host/`, read the cookie, and call every LLM-spending endpoint. Rate limit is the only remaining defense.
- **修复 / Fix**: 只在 `GET /` 已携带有效 Bearer 时才签发 cookie,或加一个一次性 `/api/login` 用 token 换 cookie。同时更新 `SECURITY.md:37`(它还在描述被删掉的 Cycle-13 meta-token 设计)。

### 🔴 P2 — 流式改写丢词典 + Reflexion 覆盖 / Streaming drops glossary + overrides (backend, high)
- **文件 / File**: `rewrite.py:1230-1261`, `web.py:1362-1366`
- **中文**: 前端默认走 `/api/rewrite-stream`(失败才退回非流式)。但流式路径既不读 `quality_store.get_override_addendum()`,也不传 `glossary_lines`。结果:你激活的自反馈覆盖、你填的品牌语调字典,在主路径上**零效果**,而且没有任何警告。整个 Cycle 9/10 Reflexion 功能 + 词典功能只在退路上生效。
- **English**: The frontend tries `/api/rewrite-stream` first. That path consults neither the activated override nor the glossary, so the entire Reflexion self-improvement loop and the brand-glossary feature are silent no-ops on the primary UX path.
- **修复 / Fix**: 把 `addendum_override` + `glossary_lines` 串进 `rewrite_stream → _build_payload`,镜像非流式 `rewrite()`;加一个集成测试断言激活覆盖后流式 system prompt 改变。

### 🔴 P3 — 流式把降级输出当正常结果 / SSE drops degraded flag (backend, high)
- **文件 / File**: `web.py:1370-1380`, `static/app.js:1415`
- **中文**: LLM 中途失败时,流式路径吐出启发式兜底文本(「给你整成东北那股劲儿:…」),但 SSE done 事件丢掉了 `degraded`/`warning`,前端还硬编码 `degraded:false`。用户在主路径上看到的是被当成正常结果的兜底文本,没有任何"已降级"提示。**没配 API key 的部署每次改写都把启发式输出当成功返回。**
- **English**: On mid-stream LLM failure the heuristic fallback is yielded but `degraded`/`warning` are dropped from the SSE done event and the frontend hardcodes `degraded:false`. Users see fallback text presented as a genuine result; a zero-API-key deployment streams heuristic output as clean success every time.
- **修复 / Fix**: 在 done 事件里带上 `degraded`/`warning`,前端读它而不是硬编码 false。

### 🔴 P4 — 节气常年显示「大寒」/ Solar term shows 大寒 all year (frontend, high)
- **文件 / File**: `static/app.js:525-545`
- **中文**: `currentSolarTerm()` 的循环按数组顺序赋值,而小寒/大寒在数组末尾,所以 2 月到 12 月**每一天**都返回「大寒」,1 月 1-5 号返回「立春」。v3 是默认主题,节气小章在每个非 daily 页面显著显示 —— 即一年约 11 个月所有用户都看到错的节气(今天 6 月显示「大寒」)。
- **English**: The loop assigns by array order; 小寒/大寒 sit at the array tail, so every date Feb 1–Dec 31 returns 大寒 (334/365 days wrong) and Jan 1–5 returns the initializer 立春. v3 is default and the chip is prominent — ~11 months of wrong 节气 for all users.
- **修复 / Fix**: 用 `key = month*100+day`,按 key 升序取最后一个 ≤ 今天的项,1 月初回绕到「冬至」;加一张月份边界单测表。

### 🔴 P5 — 广东普通话语料退化成粤语 / guangdong_mandarin collapsed into Cantonese (data, high)
- **文件 / File**: `data/corpus.jsonl`, `data/lexicon.jsonl`
- **中文**: `presets.py:389` 明确规定广东普通话「不要全篇粤字」,但 18/40 条语料是简体写的纯粤语(唔/嘅/咁/嘢/俾/蚊),跟 `cantonese_written` 高度重复(13/40 行去掉繁简差异后相似度 ≥0.8)。词表还带粤拼。`一百→一舊水`(意思是一百元不是数字 100,还混了繁体舊 + 拼音/粤拼混搭)。BM25 实测会把这些污染行喂进 prompt,与同一 prompt 里的 style_notes 自相矛盾。
- **English**: Per `presets.py:389` guangdong_mandarin must NOT be full Cantonese script, but 18/40 corpus rows are pure simplified-script Cantonese, near-duplicating `cantonese_written`; the lexicon carries jyutping. The BM25 retriever feeds these contaminated rows into the prompt, contradicting the style_notes in the same prompt.
- **修复 / Fix**: 决定契约后重生成这 40 条语料 + 118 条词表(广式普通话 = 普通话语法 + 粤味语气词/借词 + 拼音);修掉 `一舊水`。

### 🔴 P6 — 设置页保存清空弹窗设置 / Options save wipes popup settings (extension, high)
- **文件 / File**: `extension/options/options.js:55-57`
- **中文**: 保存按钮写一个全新的 `{serverUrl, encryptedToken, savedAt}` 覆盖整个 config 对象,丢掉 popup 写的 `mode` 和 `defaultVariety`。所以每次你在设置页重存(比如换 token),「选中自动改写」就静默关掉、默认方言重置回上海话,毫无提示。
- **English**: The save handler writes a fresh config object, discarding the `mode`/`defaultVariety` keys the popup wrote. Every Options re-save (e.g. token rotation) silently disables instant-selection and resets the default variety.
- **修复 / Fix**: 保存前先读旧 config 合并(`{...cur, serverUrl, encryptedToken, savedAt}`),或把 mode/defaultVariety 存到独立 key。

### 🔴 P7 — CI 从来没跑过 / CI has never run once (tooling, high)
- **文件 / File**: `.github/workflows/ci.yml:3-6`
- **中文**: workflow 触发器是 `push: branches: [main]`,但 GitHub 默认分支是 `claude/bold-bell-4750bf`,origin 上**根本没有 main 分支**,也没有任何 PR。`gh run list` 显示 CI 任务自仓库推送(2026-05-17)以来执行了**零次**。所有"198 测试绿"的信心都只是本地的;声称支持的 Python 3.10/3.11/3.12 从没被实际验证过(本地跑的是 3.14)。
- **English**: The push trigger targets `main`, which doesn't exist on origin (default branch is `claude/bold-bell-4750bf`); no PRs exist. The CI job has run zero times. All green-CI confidence is local-only; the advertised 3.10–3.12 matrix has never executed.
- **修复 / Fix**: 把触发器改成真实默认分支,或把默认分支改名 main,然后确认 `gh run list` 真出现一次运行。

### 🔴 P8 — `build_corpus.py` 实跑即崩 / build_corpus.py broken in real mode (tooling, high)
- **文件 / File**: `tools/build_corpus.py:136-165`
- **中文**: 两处独立崩溃:(1) 调用不存在的 `LLMConfig.from_env()`(真名 `load_llm_config()`)→ AttributeError;(2) 即使绕过,`general_chat(system_prompt=…, user_prompt=…)` 的真实签名是 `general_chat(messages=[...])` → TypeError,被宽 `except Exception` 吞掉,打印「✓ generated 0 new entries」退出 0。只有 `--dry-run` 能跑。
- **English**: Two breakages: nonexistent `LLMConfig.from_env()` and a wrong `general_chat()` signature, the second swallowed by a broad except printing "✓ generated 0" and exiting 0. Only `--dry-run` works.
- **修复 / Fix**: 改 `load_llm_config()` + `general_chat(messages=[...])`;total==0 时返回非零;加一个按真实签名 bind 的单测。

### 🔴 P9 — `review_corpus.py` 跳条 + 崩溃 / review_corpus.py skips entries then crashes (tooling, high)
- **文件 / File**: `tools/review_corpus.py:82,92-141`
- **中文**: `indices` 算一次,但每次审批都重写并重新加载文件,循环还在用旧 `indices`。复现:3 条文件全答 y → 第 1 条审对,第 2 条**静默跳过**,第 3 条审到时进度条显示 [2/3],最后 IndexError 崩溃。语料质量的最后一道人工关是坏的。
- **English**: `indices` is computed once but each decision rewrites+reloads the file while the loop keeps stale indices. Reproduced: a 3-row file silently skips row 2, mislabels the progress header, and crashes with IndexError. The human curation gate is broken.
- **修复 / Fix**: 不要对收缩中的文件迭代位置索引;迭代快照、按 `(variety, original)` 收集决定、最后原子重写一次;cursor 改成内容键。

### 🔴 P10 — 文档修正卡在未推送的本地 main / Doc fixes stranded on unpushed main (docs, high)
- **文件 / File**: 仓库 git 状态 / repo git state
- **中文**: 两个修正提交(`b779c19` 修正陈旧声明、`f39f58c` 删 Refiner)只在**本地未推送的 main** 上,而 GitHub 唯一分支兼默认分支 `claude/bold-bell-4750bf` 仍然对外宣称:零依赖(假,要 cryptography)、100 条语料(实际 400)、raw token 注入 HTML(假且误导安全审查)、扩展 30 分钟自动锁(已删)。违反你自己的 Rule #10(push)。
- **English**: Two correction commits live only on local unpushed `main`; the published default branch still serves four false claims (zero-deps, 100-entry corpus, raw-token-in-HTML, 30-min auto-lock). Violates the project's own push rule.
- **修复 / Fix**: 推送 main → 把 GitHub 默认分支改成 main → 删/快进旧分支。

### 🔴 P11 — Docker 镜像不带语料 / Dockerfile never copies data/ (docs, high)
- **文件 / File**: `Dockerfile:29-31`
- **中文**: Dockerfile 只 COPY `app.py`/`native_chinese_assistant/`/`static/`,**不拷 `data/`**。`corpus.py`/`lexicon.py` 找不到文件就静默返回 None(设计如此)。所以照 README 的 Docker 步骤构建出的容器,两个头条功能(few-shot 注入 + 词典 hint)**静默消失**,无报错无日志。
- **English**: The Dockerfile copies only `app.py`/package/`static/`, not `data/`. The retrievers silently return None on missing files, so a Docker build per the README produces a container where the two headline features (few-shot + lexicon) are silently absent.
- **修复 / Fix**: Dockerfile 加 `COPY data ./data`;或在 README Docker 段注明需挂载 data/;或在启动日志/healthcheck 里暴露语料缺失。

---

## 4. 对抗验证推翻/降级的发现 / Down-graded or Refuted by Verification

诚实记录:不是所有初判都成立。
- **扩展 `IDLE_CLEAR_MS` 溢出 bug —— 推翻**。怀疑 `Date.now() + Number.MAX_SAFE_INTEGER` 溢出会破坏解锁。node 实测:值虽然超过 2^53(不安全整数),但仍然有限、为正、远大于任何 `Date.now()`,两处比较仍正确,解锁流程正常。只是 code smell,不是功能 bug。
  *Extension MAX_SAFE_INTEGER overflow — REFUTED. Verified in node: unsafe integer but finite/positive/huge, comparisons still work, unlock flow intact. Code smell only.*
- **`general_chat` 5xx 重试是死代码 —— 从 high 降到 medium**。确实死(真实 transport 把 HTTPError 包成 RewriteError,重试分支永不可达),但核心 `rewrite()` 的外层 loop 意外覆盖了包装后的 5xx,30 天词池有静态兜底,所以只影响 rate/explain/characterize 在 DeepSeek 偶发 5xx 时快速失败。
  *general_chat retry dead code — high→medium. Real but the core rewrite() loop accidentally covers wrapped 5xx; only secondary endpoints fail-fast on transient blips.*
- **认证绕过从 critical 降到 high**:是真绕过,但不泄露用户数据,限流+每日上限仍兜底,SameSite=Lax+HttpOnly 防跨站。
  *Auth bypass critical→high: real but no data exposure, rate/daily caps still bound spend.*

---

## 5. 中低风险清单（择要）/ Selected Medium/Low Findings

**安全 / Security** — 语料导入工具不剥控制字符(持久化的 prompt 注入面,比已修的 characterize 更糟);override 三个改状态接口无限流;反馈 email 明文存盘(隔壁 quality store 是加密的)。

**后端 / Backend** — 每日上限在限流/校验**之前**扣费,所以 429/400 也烧配额(Cycle 21 只修了 batch,没传播到另外 5 个接口);`_REVOKED_COOKIES` 可被未认证 logout + 伪造未来时间戳无限撑大(慢速内存 DoS);今日一句的每日计费从第 2 天起失效(只查文件存在不查日期)。

**前端 / Frontend** — `$("#variety")`/`$("#glossary-textarea")` 是死 id(向导加的词条会被静默清掉);FOUC 防闪脚本把属性设在 `<html>` 但 CSS 全是 `body[data-version]` → 防闪机制无效;6 条 v3 CSS 选择器指向不存在的类名(daily/atlas 页留着 v2 深色金字配白卡,对比度不合规);`prefers-reduced-motion` 只盖了花瓣,没盖背景 Ken Burns 缩放和 JS 幻灯片;5/6 模态框无焦点陷阱;`styles.css` 大量 append-only 矛盾(`.v3-hero` 高度设了 3 次、死 `.v3-menu-btn` 整块)。

**扩展 / Extension** — 选中改写无请求代次守卫(乱序响应/已关弹窗复活);滚动会让锚定弹窗错位一个滚动距离;扩展同样静默吞 degraded;`INSTALL.md` 4 处过时(30 分钟自动锁×2、选项页打开方式、永不会出现的 HTTP 403)。

**数据 / Data** — 福建闽南语简繁混排(26/40 简体,~14 繁体);150/1189 词表行是占位符 mandarin 键(`(感叹)` 等,BM25 永远匹配不到,还会被「感谢」误触);90/400 语料标 verified 却无 source;上海话部分 verified 行 `覅`/`塔` 用错;30/40 标准普通话是原样复制(教模型"什么都不改")。

**工具/文档 / Tooling/Docs** — Playwright e2e 腐烂(默认 pytest 4 个 ERROR);smoke.sh 路径穿越检测无效(curl 发送前归一化 `../`);verify 脚本永远退出 0;README body-cap 默认值写错(16384 实为 65536);extension/README 停在「placeholder icons / awaiting install」;主仓根目录 5 个未提交散落文件(含一份与 data/ 不同的 root `corpus.jsonl`)。

---

## 6. 建议修复顺序 / Recommended Fix Order

| 批次 / Batch | 内容 / What | 理由 / Why |
|---|---|---|
| **A（now,便宜高价值)** | P4 节气 bug · P6 设置清空 · P3 流式 degraded · verify 脚本(✅已修) | 用户立即可见 / user-visible now, cheap |
| **B（一个迭代)** | P2 流式丢词典覆盖 · P1 认证收口 · P5 广东语料重生成 | 核心功能/安全正确性 / core correctness |
| **C（推送相关)** | P10 推 main + 改默认分支 · P11 Docker COPY data/ · P7 CI 触发器 | 一推就把一批文档/CI 问题一起解决 / one push closes many |
| **D（工具链)** | P8 build_corpus · P9 review_corpus · 中风险清单 | 内部工具,不影响线上 / dev tools, no prod impact |

---

## 7. 项目的真实定位 / Honest Positioning

**中文** — 这不是"又一个 ChatGPT 套壳"。它有真实的工程纪律(并发、加密、验证诊断、对抗式自审历史),核心改写功能可用,语料数据经验证是真的。但它目前是**一个单人/小团队的方言改写工具**,不是产品级多租户系统:认证模型只防得住直连 API 的人、不防能开首页的人;Docker 路径会静默丢功能;CI 从没真跑过。真正的社会价值(给"想让文字带地方魂"的中文创作者一个 LLM 现成不给的能力)成立,但**实证层面仍是 0 真实用户** —— 想验证就得先把上面 P1/P3/P4/P6 这类"一眼假"的体验 bug 修掉,否则第一个真实用户会在 30 秒内"出戏"。

**English** — This is not "another ChatGPT wrapper." Real engineering discipline, a working core, and (now verified) real corpus data. But it is currently a **single-user/small-team tool**, not a multi-tenant product: the auth model stops direct-API callers but not homepage visitors, the Docker path silently loses features, and CI has never truly run. The social premise (giving Chinese writers a regional-voice capability the base LLM won't) holds, but **empirically it has 0 real users** — to validate that, fix the "obviously-wrong" experience bugs (P1/P3/P4/P6) first, or the first real user bounces in 30 seconds.

---

*生成方式 / Generated via 7-dimension multi-agent inspection with adversarial verification. 全部发现可在 `file:line` 级复核。All findings are reproducible at `file:line` granularity.*
