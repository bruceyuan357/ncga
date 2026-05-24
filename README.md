# 地道中文 · Native Chinese Grammar Assistant (NCGA)

把任意语言的输入,揉成 10 种地区中文方言/语体(普通话、北京话、东北话、川渝话、江淮话、广东普通话、上海话风格、粤语书面语、台湾闽南语、福建闽南语)。

> **Cycle 20:**
> - **多语言输入** — 中文/英文/日文/法文… 都可,输出始终是你选的中文方言
> - **反馈表单** `/api/feedback` — 内嵌浮动按钮 + JSONL 存储
> - **双轨认证** — SPA 走 HMAC cookie,扩展/脚本走 Bearer header
> - **`/api/logout`** — cookie 撤销,无需轮转 token
> - **CSP 锁紧** — `style-src` 不再有 `unsafe-inline`
>
> **Cycle 22 v3「随四时」(已默认 · v1/v2 仍可切回):**
> - **整站浅色** — 暖白偏粉底色,告别 v2 黑底窄卡
> - **60vh 季节大图 hero**(非 daily 页)+ daily 自带 slideshow 直接 v3 化
> - **4 季调色板** — 春樱粉 #E89BB0 / 夏林绿 #4A8A5A / 秋枫橙 #D26A2F / 冬松青 #4A7B98(各带 -deep 强调 + -2 次 accent)
> - **18 朵樱花瓣全 viewport 飘落**(春日,fixed 层,所有页面都能看到)
> - **节气小章 caption** — 24 节气自动按月日定 → 立夏 / 谷雨 / 大寒…
> - **16 首古诗每日轮换** — 春夏秋冬各 4 首
> - **sidebar → 右滑抽屉** + ☰ 浮按钮
> - **页面 max-width 1100px** + workbench 双栏 + 输出卡 3px 季节色竖条
> - **settings 加「随四时」切换器** — 古朴 / 墨韵 / 随四时 三选
> - 切回 v2/v1:右下角 sidebar 底部 version-switch 点其他选项

- 后端：Python 3.10+，零运行时依赖（除 `certifi`）。WSGI。
- 前端：原生 JS / CSS / HTML，无构建步骤。
- LLM：默认 DeepSeek（OpenAI 兼容协议），可切换。LLM 不可用时回退到本地启发式重写并明示降级。

## 运行

```bash
pip install -r requirements.txt        # 仅运行
pip install -r requirements-dev.txt    # 开发 / 测试 / 生产 server

cp .env.example .env                   # 填入 DEEPSEEK_API_KEY
python app.py                          # http://127.0.0.1:8000
```

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_PROVIDER` | `deepseek` | `deepseek` 或 `openai` |
| `LLM_API_KEY` 或 `DEEPSEEK_API_KEY` | — | LLM API key（缺省走启发式 fallback） |
| `LLM_MODEL` | `deepseek-chat` / `gpt-4.1-mini` | 模型名 |
| `LLM_BASE_URL` | provider 默认 | OpenAI 兼容 base URL |
| `LLM_STREAM` | `true` | 是否使用 SSE 流式 |
| `LLM_TIMEOUT_SECONDS` | `60` | 整个请求的超时上限（秒） |
| `LLM_CA_BUNDLE` | `certifi.where()` | 自定义 CA 文件路径 |
| `LLM_SKIP_SSL_VERIFY` | `false` | **危险**：跳过 SSL 校验，仅供调试 |
| `NCGA_RATE_LIMIT_PER_MIN` | `30` | 每个 IP 每分钟可调 `/api/rewrite` 的次数（0 = 关闭） |
| `NCGA_MAX_BODY_BYTES` | `16384` | 请求体字节上限 |
| `NCGA_FEEDBACK_RATE_LIMIT_PER_MIN` | `5` | Cycle 20: 每个 IP 每分钟可提交 `/api/feedback` 的次数 |
| `NCGA_FEEDBACK_MAX_BODY_BYTES` | `8192` | Cycle 20: 反馈表单请求体上限 |
| `NCGA_FEEDBACK_STORE` | `~/.local/share/ncga/feedback.jsonl` | Cycle 20: 反馈 JSONL 落地路径 (`0o600`) |
| `NCGA_AUTH_TOKEN` | — | Cycle 13/20: 设置即开启双轨认证(SPA cookie + Bearer);清空即完全无 auth |
| `NCGA_TRUST_FORWARDED_FOR` | `false` | 是否信任 `X-Forwarded-For`(部署在 cloudflared/Nginx 后面时设 `true`) |
| `NCGA_HOST` | `127.0.0.1` | 监听地址 |
| `NCGA_PORT` | `8000` | 监听端口 |
| `NCGA_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `NCGA_CORPUS_PATH` | `data/corpus.jsonl` | Cycle 22 Stage C: 语料库 JSONL 路径(覆盖默认) |
| `NCGA_CORPUS_DISABLE` | — | Cycle 22 Stage C: 设 `1` 关掉 few-shot 注入(回到无示例 prompt) |

## 语料库与少样本注入 (Cycle 22 Stage C)

`data/corpus.jsonl` 是手写的方言语料(100 条:10 方言 × 10 场景),每条:
```json
{"variety":"shanghai_mandarin_style","scenario":"request","original":"我能借一下你的笔吗","rewrite":"支笔借拨我用一道好伐","quality_tier":"verified","notes":"拨+用一道+伐"}
```

每次 `/api/rewrite` 调用时,后端用纯 stdlib BM25(`native_chinese_assistant/corpus.py`)从目标方言池里检索 top-3 最像的示例,以「【本地人示例】参考这些真实的本地说法,不要逐字复制」块注入 system prompt。LLM 因此能看到具体的「原文 → 本地说法」对子,而非仅靠风格描述脑补。

**当前状态**:70 条 verified(普通话/京/东北/川渝/江淮/广普/上海)+ 30 条 needs_review(粤书/台闽南/福建闽南 — 母语者欢迎 PR)。

**扩到 30 条/方言**(需 `DEEPSEEK_API_KEY`):
```bash
python3 tools/build_corpus.py --target 30
python3 tools/review_corpus.py        # 交互 y/n/e/s/q 审批
```
审批通过的进 `data/corpus.jsonl`,拒绝的进 `data/corpus_rejected.jsonl` 留作审计。

**临时关掉**:`NCGA_CORPUS_DISABLE=1 python3 app.py`。

### 从外部 deep research 增量导入 (Stage D)

Cowork / Perplexity Deep Research / Gemini Deep Research 拉回来的 `corpus.jsonl`:
```bash
python3 tools/import_corpus.py /path/to/incoming.jsonl --dry-run    # 先验证
python3 tools/import_corpus.py /path/to/incoming.jsonl              # 正式 append
```
自动按 `(variety, original)` 去重,reject schema 不合规的行,输出 stats。

## 词音对应表 lexicon (Cycle 22 Stage D)

`data/lexicon.jsonl` 是词典级别的「普通话词 → 方言词」对应,每条:
```json
{"variety":"shanghai_mandarin_style","mandarin":"漂亮","local":"嗲","category":"idiom","ipa":"tia44","example_sentence":"侬嗲伐","source":"https://wiktionary.org/wiki/嗲"}
```

与 corpus 平行存在:**corpus 给 LLM 句子级 few-shot,lexicon 给词级 hint**。
每次 `/api/rewrite` 调用时同样用 BM25 检索 top-5,以「【词音参考】」块注入 system prompt
(口吻:"如果能自然用上几个就用,不要强塞")。

**导入 Cowork 给的 lexicon**:
```bash
python3 tools/import_lexicon.py /path/to/incoming.jsonl --dry-run
python3 tools/import_lexicon.py /path/to/incoming.jsonl
```
按 `(variety, mandarin, local)` 主键去重。

**抽查质量**:
```bash
python3 tools/verify_corpus_quality.py --corpus 20 --lexicon 20
```
随机抽 N 条做 HTTP HEAD 检查 source URL 是否可访问;按 variety breakdown verified 率。

**临时关掉**:`NCGA_LEXICON_DISABLE=1 python3 app.py`。

## 测试

```bash
python -m unittest discover tests
# 或
pytest
```

测试**完全离线**——LLM 请求被注入 fake client。

### 真 LLM 冒烟测试

离线测试覆盖不到「LLM 端协议变了」这一类回归（Cycle 17 发现 DeepSeek-V4 升级后 `/api/rate` 因 `max_tokens` 太小静默坏掉，离线 mock 测试全绿）。每次发布后跑一次：

```bash
tools/smoke.sh                           # 默认 http://127.0.0.1:8000
BASE=https://ncga.example.com tools/smoke.sh
```

`tools/smoke.sh` 会真调 LLM（每次几分钱），覆盖 14 项：所有 GET、auth gating、3 个 LLM 走通、Cycle 16 的 4xx 诊断。建议挂 cron 每日跑一次。

## 生产部署

`wsgiref.simple_server` 是开发服务器。生产用 `waitress`：

```bash
pip install waitress
waitress-serve --host 0.0.0.0 --port 8000 native_chinese_assistant.web:application
```

或 `gunicorn` / `uvicorn --interface wsgi`。**务必**在反代层（Caddy / nginx）做 TLS 与额外限流。

## 安全 / Security

详见 [SECURITY.md](SECURITY.md)。**公网部署前**至少做以下三件：

1. **TLS 反代**（Caddy / Nginx / Cloudflare）。NCGA 自身只跑 HTTP，永远不要直接暴露到公网。
2. **设 `NCGA_AUTH_TOKEN`**：所有 `POST /api/*` 走 bearer 鉴权，前端通过服务端注入的 `<meta>` 自动附 `Authorization` 头。
3. **设 `NCGA_DATA_KEY`**：质量存储 AES-GCM 落盘（包含用户原文 + 评分等敏感数据）。

```bash
# 生成两个密钥
NCGA_AUTH_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
NCGA_DATA_KEY=$(python3 -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())")
echo "NCGA_AUTH_TOKEN=$NCGA_AUTH_TOKEN" >> .env
echo "NCGA_DATA_KEY=$NCGA_DATA_KEY" >> .env
```

| 控制项 | 默认 | 注 |
|---|---|---|
| 限流 30/min/IP | ✅ on | 反代层应再加一层 |
| Body cap 64KB | ✅ on | 防 OOM |
| 路径穿越拒绝 | ✅ on | 多种编码全部 404 |
| CSP（无 `'unsafe-inline'` script-src） | ✅ on | 内联引导脚本走 nonce |
| HSTS | ✅ on | 反代层 TLS 时生效 |
| Bearer auth | ⚪ 默认关 | 设 `NCGA_AUTH_TOKEN` 开 |
| Quality store AES-GCM | ⚪ 默认关 | 设 `NCGA_DATA_KEY` 开（或自动落地到 `~/.local/share/ncga/`） |
| X-Forwarded-For 信任 | ⚪ 默认关 | 反代后才设 `NCGA_TRUST_FORWARDED_FOR=true` |

## Docker 部署

```bash
docker build -t ncga .
docker run -d --name ncga --restart unless-stopped \
  --env-file .env \
  -p 127.0.0.1:8000:8000 \
  -v ncga-data:/home/ncga/.local/share/ncga \
  ncga
```

容器以非 root 用户跑（uid 1000），用 waitress 多线程 serve，tini 做 PID 1 接信号。
**前面必须套 TLS 反代**——容器只暴露 HTTP。

## 架构速览

```
app.py
└── native_chinese_assistant/
    ├── web.py        # WSGI 路由 / 限流 / 安全头
    ├── rewrite.py    # LLM 客户端 + 启发式 fallback + RewriteService
    └── presets.py    # 所有方言元数据（label / register / style / landmarks / keywords / letter）
static/
├── index.html
├── app.js            # 单文件前端，所有方言数据从 /api/presets 拉
└── styles.css
tests/test_app.py     # 离线测试，mock 掉 LLM HTTP
```

## 加一种新方言

只改 [`presets.py`](native_chinese_assistant/presets.py) 一处：在 `VarietyPreset` 加 enum，然后在 `PRESET_METADATA` 加完整元数据（label/register/style_notes/keywords/landmarks/letter/description）。前端会自动加载。

## Cycle 20 · 多语言输入

任何输入语言都被接受 — 系统提示词锁定:
- **输出语言锁定**:不论用户用中文/英文/日文/法文输入,输出必须是中文方言文字(人名/地名/URL 除外)
- **跨语言一致性**:同一个意思,无论用哪种语言提问,输出都应高度一致;方言版本由【说话内容】决定,不被【输入语言形式】影响

```bash
# 英文输入 → 北京话
curl -X POST http://127.0.0.1:8000/api/rewrite \
  -H "Authorization: Bearer $NCGA_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Hello, how is your day?","target_variety":"beijing_mandarin"}'
# → {"rewritten_text": "嗨喽,今儿过得咋样啊?", ...}
```

离线 heuristic 兜底针对非中文输入会**诚实拒绝**(不再"给你整成东北那股劲儿:very good"那种翻车)。

## Cycle 20 · 反馈表单

内嵌的「💬 反馈」浮动按钮,采集星级 + 标签 + 自由文字 + 联系方式 → 落地 `~/.local/share/ncga/feedback.jsonl` (模式 `0o600`)。

```bash
# 实时看反馈进来
tail -f ~/.local/share/ncga/feedback.jsonl

# 评分分布
jq -s 'group_by(.rating)|map({rating:.[0].rating,n:length})' \
  ~/.local/share/ncga/feedback.jsonl

# 抓自由文字
jq -r 'select(.note != "") | "[\(.rating)★] \(.note)"' \
  ~/.local/share/ncga/feedback.jsonl
```

设计要点:
- IP 用 salted SHA-256 (`_hash_ip`),**不存原始 IP**
- 控制字符(`\r\n\t` + ANSI escape + DEL)在落地前剥掉 → 防 `tail` 操作员被 terminal injection 攻击
- 5 次/分钟/IP 限流;无 LLM 调用,不进 daily LLM 上限
- 文件 `0o600`,本机其它用户读不到

## Cycle 20 · 双轨认证 (HMAC cookie + Bearer token)

| 场景 | 认证轨道 | 怎么传 |
|---|---|---|
| 浏览器 SPA | HMAC-signed cookie | 自动 (`credentials: same-origin`) |
| Chrome 扩展 / curl / 脚本 | Bearer token | `Authorization: Bearer $NCGA_AUTH_TOKEN` |

`_check_auth` 任一通过即放行。一个写定的设计点:**cookie 用 `NCGA_AUTH_TOKEN` 当签名密钥** → 轮转 token 立刻吊销所有 cookie。

cookie 形态:`<unix_timestamp>.<random_nonce>.<hmac_sha256_24>`。无服务端 session 表。

撤销机制(self-audit #6):内存 `_REVOKED_COOKIES` set,30 天自动 GC。`POST /api/logout`:
```bash
curl -X POST http://127.0.0.1:8000/api/logout \
  --cookie "ncga_sess=<your_cookie>"
# → {"ok": true, "revoked": true}
# Set-Cookie: ncga_sess=; Max-Age=0 让浏览器丢 cookie
```

## 许可

MIT
