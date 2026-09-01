# V2EX 发布草稿（分享创造节点）

**标题：**
[开源] 地道中文 NCGA：把书面普通话改写成十种方言口感，带质量评测门禁和自带 Key

**正文：**

造了个轮子，解决一个我自己天天碰到的问题：给父母长辈、给不同地区的朋友
发消息，书面普通话总差点意思。市面上的翻译器做的是「普通话 ↔ 方言词汇
替换」，结果是「技术上没错，但一看就是外地人」。

**做法上几个比较硬的点：**

- 后端纯 Python 标准库（WSGI 手写，无 Flask/FastAPI），运行时仅
  `certifi` + `cryptography` 两个包；前端原生 CSS/JS 无构建步骤。
- 方言质量不是玄学：每种方言有 golden set，评审用锚定 LLM judge
  （temperature 0、中位数取值），任何改动导致某方言掉分超过 0.3 直接
  CI 失败。目前 10 种方言基线均分 4.00/5。
- 按方言路由模型：简单方言走 DeepSeek V4 Flash，难方言（粤语书面语、
  闽南语等）自动切 Pro。A/B 实验跑出来的结论是：CoT 对方言改写没帮助，
  模型档位才是唯一的质量杠杆 —— 细节在仓库的 eval 文档里。
- BYOK：不找部署者要令牌，设置里粘贴自己的 DeepSeek key 就能用全部功
  能。key 只存浏览器 localStorage，随请求直传上游，服务器不记录。自带
  key 的请求不占部署者的每日额度。
- 诚实降级：上游返回空流时回退启发式改写，UI 和 API 都带
  `degraded: true` 标记，不装没发生。

**功能：** 流式改写、批量工作台（100 条 × 4 方言扇出）、情境向导（两句话
生成语域画像）、每日方言一句（配地标摄影）、润色/中英互译/总结/白话解释、
实时质量面板（Welford 在线统计 + 加密存储）。

**安全方面做了点功课：** Bearer + HMAC cookie 双轨鉴权、每 IP 每日 LLM
额度、AES-GCM 加密的质量数据、CSP/nonce、路径穿越防护，都有对应测试。

GitHub: https://github.com/bruceyuan357/ncga （MIT）
本地跑起来只需要：`pip install -r requirements.txt`（就 certifi +
cryptography 两个包）、`cp .env.example .env` 填 DeepSeek key，
然后 `python app.py`。

欢迎拍砖，特别是方言母语者 —— 评测集里 needs_review 的条目就等你们了。
