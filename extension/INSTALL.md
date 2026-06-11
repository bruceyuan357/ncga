# 地道中文 Chrome 扩展 — 安装验收清单

> 给 Bruce:你回来后照这个清单从上往下走,每步若失败截图给 Claude,问"我卡在第 X 步"即可。

---

## 0. 前提条件

| 条件 | 怎么检查 |
|---|---|
| 服务器在跑 | Terminal 进 `/Users/bruce/NCGA/.claude/worktrees/bold-bell-4750bf` 后跑 `python3 app.py`,看到 `Listening on http://127.0.0.1:8000` 就 OK |
| 服务器开了 token | 跑 `grep NCGA_AUTH_TOKEN .env`,有一行像 `NCGA_AUTH_TOKEN=xxxxxxxx` |
| 没 token? | 编辑 `.env` 加一行 `NCGA_AUTH_TOKEN=` + 一段你自定义的字符串(32 位以上,随便打),保存,**重启 app** |
| Chrome 是新版 | 地址栏输 `chrome://version`,要 ≥ 116 |

---

## 1. 加载扩展(开发模式)

1. Chrome 地址栏输 `chrome://extensions`
2. 右上角 **开发者模式** 切到开
3. 左上角点 **加载已解压的扩展程序**
4. 选择 `/Users/bruce/NCGA/.claude/worktrees/bold-bell-4750bf/extension` 文件夹
5. 应该看到一张卡:**地道中文 · NCGA  v0.1.0**(带粉色樱花图标)

✅ **预期:** 卡片底色白,**没有红色错误提示**

❌ **若有红色** "Could not load icon ..." → icons 文件夹缺图,跑:
```bash
cd /Users/bruce/NCGA/.claude/worktrees/bold-bell-4750bf && ls extension/icons/
```
应该 4 个 PNG,缺了告诉我,我用脚本重生成

❌ **若有红色** "Manifest version 3 ..." → 升级 Chrome 到 116+

---

## 2. 固定扩展到工具栏

1. Chrome 右上角拼图按钮(扩展图标)
2. 找到「地道中文」一行,点旁边的图钉
3. 现在工具栏上应该有一朵粉色小樱花

---

## 3. 设置 — 填服务器和 token

1. 工具栏樱花图标 → 弹窗 → 底部点 **设置**
2. Options 以内嵌对话框形式在 `chrome://extensions` 页面里打开(`open_in_tab: false`,不是新标签页)
3. 填:
   - **服务器 URL:** `http://localhost:8000`(已默认)
   - **Token:** 把 `.env` 里 `NCGA_AUTH_TOKEN=` 后面的字符串粘进来
   - **passphrase:** 一段你记得住的话,至少 6 个字(例如 `mySpring2026!`)
   - **再输一次:** 同样
4. 点 **加密并保存**
5. 看到绿色 `✓ 已加密保存。打开扩展弹窗输 passphrase 解锁即可使用。`
6. **当前状态** 框应该显示:
   ```
   serverUrl:        http://localhost:8000
   encryptedToken:   v1.xxxxxxxxxxxxxxxxxx…
   token blob len:   ~140 chars
   saved at:         2026-05-...
   ```

✅ **预期:** token 输入框自动清空(安全设计 — 不留页面残影)

---

## 4. 解锁

1. 工具栏樱花图标 → 弹窗
2. 顶上一行:**Token 已锁定 — 输 passphrase 解锁(关浏览器才会锁)**
3. 输你刚才设的 passphrase → **解锁**
4. 弹窗变成:**已就绪 · http://localhost:8000**(绿色)

❌ **若失败** "解锁失败:passphrase 错误?" → 重新打开 Options 重设(passphrase 输错了)

---

## 5. 测试场景 A — 弹窗内改写

1. 弹窗里粘贴 `今天天气真好,我们一起去公园吧`
2. 选「上海话风格」
3. 点 **改写**
4. 几秒后下方出现改写结果

✅ **预期:** 看到改写后的文字。如果服务器在跑、token 对、网络通,这一步必出。

---

## 6. 测试场景 B — 右键菜单(主要使用方式)

1. 打开任何网页(比如 https://example.com 或新建 tab → about:blank → 写点字)
2. 选中一段文字
3. 右键 → 应该看到 **改写为(地道中文)** ▶
4. hover → 看到 10 个方言选项
5. 点其中一个(比如「东北话」)
6. 屏幕右下角弹出粉色浮窗:
   - 顶上「地道中文」+ 当前方言粉 chip + × 关闭
   - 中间显原文(超过 80 字可展开)
   - 衬线宋大字显改写结果
   - 底部 **复制改写** 按钮
7. 按 **ESC** 或 **×** 关闭浮窗

✅ **预期:** Shadow DOM 隔离 — 浮窗样式完全不受网页 CSS 影响

---

## 7. 测试场景 C — 复制 + Esc + 重锁

1. 改写出结果后 → 点 **复制改写** → 按钮变 **已复制 ✓**(1.5s 后还原)
2. 在任何地方 ⌘V 粘贴 → 验证复制内容是改写结果
3. ESC 关浮窗
4. 工具栏樱花图标 → 弹窗 → 底部 **锁回去** → 立刻锁

✅ **预期:** 锁回去后再点弹窗,又显「Token 已锁定」面板

---

## 8. 锁定机制(关浏览器才会锁)

- 解锁后 token 缓存在 `chrome.storage.session`,**整个浏览器 session 内有效**——不会因为放置不用而自动锁(以前的 30 分钟自动锁已去掉,反复输 passphrase 太烦)
- 关掉所有浏览器窗口 → session storage 自动清空 → 下次打开要重输 passphrase
- 想立刻锁:扩展弹窗底部「锁回去」

---

## 9. 常见错误 → 怎么救

| 错误 | 原因 | 救法 |
|---|---|---|
| 右键没有「改写为」 | 没选中文字 → contextMenus 仅在 selection contexts 显示 | 先选中文字再右键 |
| 浮窗显 ✗ HTTP 401 | token 错或被服务器换了 | 重新打开 Options 填新 token |
| 浮窗显 ✗ Failed to fetch | 服务器没在跑 / URL 写错 | `python3 app.py` 跑起来,检查 Options URL |
| 浮窗显 ✗ HTTP 429 | 服务器 IP-rate-limit 命中(默认每 IP 每分钟 30 次) | 等 1 分钟 |
| popup 显「未配置」但 Options 已存 | Options 没真存盘 | 重开 Options,确认 **当前状态** 框有数据,没数据再点 **加密并保存** 一次 |
| 改写一直转圈 | 服务器卡了 / LLM 慢 | 看 service worker 日志:`chrome://extensions` → 找 NCGA → 点 **Service worker** → console 看错误 |
| 完全没反应 | 扩展崩了 | `chrome://extensions` → NCGA 卡片 → 圆形刷新箭头点一下 |

---

## 10. 卸载

1. `chrome://extensions` → NCGA 卡片 → 移除
2. 这会清掉 `chrome.storage.local`(encrypted token blob 一起没了)
3. Options 设过的 passphrase 本来就没存

---

## 11. 升级 / 修代码后

```bash
cd /Users/bruce/NCGA/.claude/worktrees/bold-bell-4750bf
git pull             # 拉 Claude 推的最新改动
```
然后 `chrome://extensions` → NCGA → 点刷新箭头,新代码生效。

---

## 12. 把扩展推给别人

不推荐。这是开发版,没过 Chrome Web Store 审查,host_permissions 写死 localhost,
别人装了也不会工作。如果要发布,得:
1. 把图标换成真 logo
2. host_permissions 加生产域名
3. 用 zip 打包整个 extension/ 目录
4. 上传 Chrome Web Store(一次性 $5,审 1-3 天)
5. 用户从 Store 装

---

## 卡住时

照 Section 9 表查,查不到 → 截图 + 「我卡在第 X 步」给 Claude。
