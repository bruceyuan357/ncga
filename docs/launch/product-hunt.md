# Product Hunt launch draft

**Tagline (EN):**
Write Mandarin like a native — in 10 dialects, not textbook Chinese.

**Tagline (CN):**
把书面普通话，改写成十种地道的方言口感。

**Description (EN):**

NCGA (地道中文) is a local-first web app that rewrites standard Mandarin into
how people actually talk — Beijing, Dongbei, Sichuan/Chongqing, Jianghuai,
Cantonese-flavored Mandarin, Shanghainese style, written Cantonese, Hokkien,
and more.

What makes it different:

- **Dialect quality is measured, not vibes.** Every variety ships with a golden
  eval set and an anchored LLM judge; regressions fail the gate, so "地道"
  is a number we defend, not a claim in a README.
- **Model routing per variety.** Easy varieties run on DeepSeek V4 Flash for
  speed and cost; hard ones (written Cantonese, Hokkien) automatically route
  to the Pro tier. You see which model answered.
- **BYOK.** Paste your own DeepSeek key in Settings and every feature runs on
  your account. No sign-up, no token from the operator — the key never leaves
  your browser except straight upstream to DeepSeek.
- **Honest failure.** If the LLM is unreachable, the app says so — a clear
  error in the UI and a 503 from the API — instead of quietly handing you a
  worse result dressed up as a real one.
- **Near-zero dependencies.** The backend is Python standard library — the only
  runtime packages are `certifi` (TLS roots) and `cryptography` (at-rest
  encryption). Clone, add a key, run `python app.py`.

Also in the box: streaming rewrites, a batch workbench, scenario wizard
(tell it "my 80-year-old Shanghainese grandma" and it picks the register),
a daily dialect phrase with landmark photography, and a live quality
dashboard with per-variety scores and latency.

MIT licensed. Self-hosted. Your history never leaves your browser.

**First comment (maker):**

I built this because translating into dialect is not the same as translating
into English — the failure mode is "technically correct, obviously a
foreigner". The whole architecture is built around catching that: golden
sets, an anchored judge, per-variety model routing, and honest error reporting
that admits when it's degraded. Happy to answer anything about the eval
protocol or the BYOK design.

---

**Description (CN):**

地道中文（NCGA）是一个本地优先的网页应用：把书面普通话改写成十种真实
的方言口感 —— 北京、东北、川渝、江淮、广普、上海话风格、粤语书面语、
闽南语等。

不一样的地方：

- **地道程度是可度量的。** 每种方言带黄金评测集 + 锚定评审模型，质量回
  归会直接卡住流水线，「地道」是我们守住的分数线，不是宣传语。
- **按方言路由模型。** 简单方言走 DeepSeek V4 Flash（快、便宜），难方言
  （粤语书面语、闽南语）自动路由到 Pro。界面会告诉你用的是哪个模型。
- **自带 Key（BYOK）。** 在设置里粘贴你自己的 DeepSeek key，所有功能走
  你的账户。不需要注册，key 只存在你浏览器里，请求直传 DeepSeek。
- **诚实降级。** 上游抖动时回退到启发式改写，并在界面和 API 里明确标注
  「已降级」，而不是悄悄给你一个更差的结果。
- **近乎零依赖后端。** 纯 Python 标准库，运行时仅 `certifi`（TLS 根证
  书）和 `cryptography`（落盘加密）两个包。克隆、填 key、
  `python app.py`，完事。

还有：流式改写、批量工作台、情境向导（告诉它「我 80 岁的上海奶奶」，它
自己选语域）、每日方言一句配地标摄影、实时质量面板。

MIT 协议，可自托管，历史记录只存在你的浏览器里。
