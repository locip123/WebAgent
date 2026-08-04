# Scrapling / cloakFetch 与 WebRetriever 的可行性调研

日期：2026-08-04

## 结论

| 方案 | 正式评测可行性 | 结论 |
|---|---:|---|
| 直接用 Scrapling 0.4.12 替换现有浏览器运行时 | 低 | 虽支持 `cdp_url`，但会另建 CDP 连接、BrowserContext 和 Page，默认不复用当前任务会话，且动作不会自动进入项目现有轨迹链路。 |
| 改造 Scrapling，使其接收现有 Page/Context | 中 | 技术上可行，但改造面不小；必须补 CDP 鉴权、上下文所有权、有限状态验证和完整轨迹记录。 |
| 只复用 Patchright，或移植 Scrapling 的检测/点击思路 | 高 | 最贴合现有架构。保留 `BrowserRuntime` 为唯一控制器，验证操作仍走已有截图、动作和网络记录。 |
| 只用 Scrapling `Selector` 解析官方页面 HTML | 高 | 不产生额外网络或浏览器动作，是风险最低的用法。 |
| 使用 cloakFetch 处理正式任务 | 不可行 | 它启动独立 CloakBrowser，不接收官方 CDP；而且官方文档明确不支持需要点击的交互式 Turnstile。 |

第三方框架本身并不违规：比赛指南允许 Browser Use、Playwright 或自有框架，条件是接收官方 CDP URL并按格式输出；同时会检查浏览器操作轨迹，且禁止搜索引擎。因此判断标准不是项目名称，而是实际执行路径是否仍在官方浏览器和可审计轨迹内。[WebRetriever Guide](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/)

## Scrapling 0.4.12

### 有利条件

- 当前版本的浏览器扩展依赖 `playwright==1.61.0` 和 `patchright==1.61.2`；本项目也使用 Playwright 1.61.0。`pip --dry-run` 未发现 Playwright 版本冲突。[Scrapling pyproject](https://github.com/D4Vinci/Scrapling/blob/main/pyproject.toml)
- `StealthyFetcher`/`StealthySession` 已支持 `cdp_url`，所以“Scrapling 完全不能接远程浏览器”这一判断已不成立。[Scrapling README](https://github.com/D4Vinci/Scrapling)
- `Selector` 可以只处理已经由官方 Playwright Page 得到的 HTML；这种离线解析不会改变浏览器或访问路径。

### 不能直接接入的原因

1. **CDP 鉴权不完整。** 本项目连接云沙箱时会从 URL 提取 `access_token`，向 `connect_over_cdp` 传入 `X-Access-Token`；Scrapling 当前的 CDP 分支只传 endpoint URL，未提供对应 headers。[Scrapling stealth engine](https://github.com/D4Vinci/Scrapling/blob/main/scrapling/engines/_browsers/_stealth.py)
2. **会话不连续。** 本项目优先复用 `browser.contexts[0]`；Scrapling 连接后调用 `browser.new_context(...)` 并创建自己的 Page。这样会丢失官方上下文中的 cookie、挑战通行状态和页面所有权。
3. **轨迹会断层。** Scrapling 返回自己的 `Response`/Selector，而不是把 Page 交给现有 `BrowserRuntime`。它内部完成的导航、鼠标点击、等待和网络活动不会自动写入当前 `trajectory/`、`trajectory_visual/` 与 `capture.json`。
4. **Cloudflare solver 不适合当前失败模式。** 其 solver 会检测 challenge iframe/checkbox 并点击，但挑战仍存在时会递归再次求解，缺少整个求解过程的明确重试上限。对已经观察到的 “Verifying...” 重置循环，这可能退化成连续点击/递归，而不是解决根因。
5. **默认配置有额外风险。** `google_search=True` 会设置 Google Referer；即使它没有调用搜索引擎，也不应在比赛路径中保留。headless 条件下还可能生成新的 User-Agent/viewport 等上下文属性，使其与官方已启动浏览器不一致。
6. **生命周期冲突。** Scrapling Session 自己管理并关闭 context/browser/Playwright；直接嵌入时必须区分“外部拥有”的官方连接，否则可能影响 worker 后续任务。

### 如果坚持完整集成，最低改造清单

1. 给 Scrapling 增加接受现有异步 `BrowserContext` 或 `Page` 的入口；不要新建第二个上下文。
2. 若仍自行连接 CDP，传入本项目的 `cdp_headers_for_url(cdp_url)`，并优先使用 `browser.contexts[0]`。
3. 对外部传入的 browser/context/page 不执行 close。
4. 设置 `google_search=False`，禁用代理和会改变官方浏览器身份的 UA/viewport/profile 生成。
5. 把每次导航、鼠标点击、键盘输入、等待、截图及网络事件接入现有轨迹写入器。
6. 把递归 solver 改成有限状态机：识别挑战 → 最多一次点击 → 等待页面稳定（例如最多 30 秒）→ 最多两次挑战重置；超过上限即记录失败证据，不无限点击。

上述工作完成前，不建议在正式评测入口中调用 `StealthyFetcher.fetch()`。

## cloakFetch

cloakFetch 是面向 Claude Code `WebFetch` 失败后的 hook/skill，而不是通用浏览器 Agent 框架。它的脚本调用 `cloakbrowser.launch(headless=True)`，新建自己的页面，获取完整 HTML 后用 Trafilatura 输出 Markdown；没有 `cdp_url` 参数，也没有复用 Playwright Page 或输出 WebRetriever 所需轨迹的接口。[cloakFetch README](https://github.com/Agents365-ai/cloakFetch) [cloak_fetch.py](https://github.com/Agents365-ai/cloakFetch/blob/main/hooks/cloak_fetch.py)

更关键的是，其官方 `SKILL.md` 明确把“需要点击的 Cloudflare Turnstile checkbox”列为不支持场景；脚本只轮询 `Just a moment...` 页面标题，最多等待一段时间，并不点击验证控件。[cloakFetch SKILL.md](https://github.com/Agents365-ai/cloakFetch/blob/main/skills/cloak-fetch/SKILL.md)

因此 cloakFetch 也许适合在获授权的普通采集任务中绕过被动指纹检查，但对本次出现交互式 checkbox 的任务既不能解决核心问题，也不满足正式评测的官方 CDP 和轨迹要求。

## 推荐落地方式

```text
官方 CDP 浏览器
  → BrowserRuntime（唯一的导航、点击、输入、截图和网络捕获入口）
      → 有界 Cloudflare 验证状态机（可借鉴 Scrapling 检测逻辑）
      → 当前 Page 的 HTML 快照
          → 可选 Scrapling Selector（仅离线结构化解析）
```

优先做一个小规模 A/B：现有 Playwright 1.61.0 与直接替换为兼容版 Patchright，各跑相同的公开任务和同一套有界验证策略，比较挑战通过率、轨迹完整性、页面上下文是否连续以及普通站点回归。Scrapling 全框架只有在这个较小实验仍无法满足需求时才值得改造；cloakFetch 不进入正式评测候选。

## 来源

- [WebRetriever Challenge Guide](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/)
- [Scrapling GitHub](https://github.com/D4Vinci/Scrapling)
- [Scrapling browser engine source](https://github.com/D4Vinci/Scrapling/blob/main/scrapling/engines/_browsers/_stealth.py)
- [Scrapling dependency metadata](https://github.com/D4Vinci/Scrapling/blob/main/pyproject.toml)
- [cloakFetch GitHub](https://github.com/Agents365-ai/cloakFetch)
- [cloakFetch fallback script](https://github.com/Agents365-ai/cloakFetch/blob/main/hooks/cloak_fetch.py)
- [cloakFetch skill limitations](https://github.com/Agents365-ai/cloakFetch/blob/main/skills/cloak-fetch/SKILL.md)
- [Cloudflare challenge limitations](https://developers.cloudflare.com/cloudflare-challenges/concepts/how-challenges-work/#limitations)
