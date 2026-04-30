# 地道中文 · Native Chinese Grammar Assistant (NCGA)

把任意中文文本，揉成 10 种地区方言/语体（普通话、北京话、东北话、川渝话、江淮话、广东普通话、上海话风格、粤语书面语、台湾闽南语、福建闽南语）。

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
| `NCGA_HOST` | `127.0.0.1` | 监听地址 |
| `NCGA_PORT` | `8000` | 监听端口 |
| `NCGA_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

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

## 许可

MIT
