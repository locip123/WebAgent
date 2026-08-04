# Cloudflare 拦截：合规开源项目与 WebRetriever 选型

核验日期：2026-08-04。本调研只讨论已经获得网站所有者、账号所有者或赛事规则授权时的自动化与接入；**不推荐也不描述**以外部代解、指纹伪装、stealth 补丁、代理轮换或伪造会话的方式绕过 Cloudflare Challenge/Turnstile 或 CAPTCHA。

> **赛事补充解释（用户于 2026-08-04 转述）**：真实网站中出现的反爬/风控和需要人工点击的验证，是比赛 Web 环境的一部分；Agent 应在官方浏览器内解决这类验证。以下“排除项”因此只排除改变访问身份或把验证外包的方案，**不排除**在页面内通过 Playwright 对可见验证控件进行正常、可审计的点击和等待。

## 结论先行

对本仓库的 WebRetriever Challenge，正确选型是继续用现有的 **标准 Playwright**，而不是接入另一个“反拦截”框架。正式评测要求：官方通过 CDP 提供浏览器，且全部浏览器交互必须经 Playwright；Protocol III 还禁止外部搜索引擎。[赛事 Guide「如何备赛」](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#prepare)；[FAQ](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#faq)。

Cloudflare 的公开 Challenge 表示网站要求完成其安全校验，并不等价于“缺少一个开源绕过库”。在本赛事中，第一优先级是由 Agent 在官方浏览器内完成页面可见的验证交互；另外两类合规接入路径为：

1. 以低频、可审计的 Playwright 操作识别验证页、点击当前可见且语义匹配的控件，并等待页面明确的成功或失败状态；
2. 在已有授权的正常浏览器会话中读取已成功返回的数据；
3. 获得网站发布者提供的第一方 API、数据下载或正式账号授权后，按其文档接入；
4. 若目标是自己管理的 Cloudflare Access 应用，由管理员配置 Service Auth / OAuth 等授权，而非让客户端规避校验。

对题目给出的失败记录，优先级更高的改进不是换框架：`result.json` 记载在进入 Challenge 前已有站内请求 `119` 的 `200` 响应，且任务随后把大量步骤消耗在重复点击/等待验证页。应先让 Agent 识别验证页、对可见验证控件执行一次语义定位后的正常点击，并等待明确状态变化；同时在**每个 Playwright 响应到达时**立即落盘并解析与当前题目相关的、已实际成功返回的第一方 JSON。若同一验证控件已点击但页面无状态变化，则保留证据、有限退避并停止无效重复。这一判断只基于本地轨迹，不把轨迹中未提取出的值当作答案。[任务 69 运行记录](../../outputs/protocol3_10/69_714f9fbc74a94c8ebbacfc72f223c85f/result.json)。

## 候选项目与适配性

| 项目 | 成熟度与许可证（以官方仓库/文档为准） | 合规可用能力 | 对本 Challenge 的结论 |
| --- | --- | --- | --- |
| [Microsoft Playwright](https://github.com/microsoft/playwright) | **高**：Microsoft 维护、跨 Chromium/Firefox/WebKit 的统一自动化 API，并持续发布版本；源码为 [Apache-2.0](https://github.com/microsoft/playwright/blob/main/LICENSE)。 | `BrowserContext` 隔离会话、保存/恢复 `storage_state`、网络响应监听、截图和 trace。官方 Python 文档明确支持从已认证 context 保存并在新 context 复用状态；状态文件可能含可冒用的 cookie/headers，必须置于 Git 忽略目录和受控密钥存储中。[认证与状态复用](https://playwright.dev/python/docs/auth)；[BrowserContext API](https://playwright.dev/docs/api/class-browsercontext)。 | **唯一应直接采用的浏览器底座。**连接赛事传入 CDP，持续复用同一题的 context；只持久化经网站明确允许、由合法账号产生的授权状态。 |
| [Apify Crawlee](https://github.com/apify/crawlee) | **高（通用爬取框架）**：官方仓库采用 [Apache-2.0](https://github.com/apify/crawlee/blob/master/LICENSE.md)，提供 Playwright crawler、持久队列、会话管理、路由与重试；仓库有连续发布历史。[官方 README](https://github.com/apify/crawlee)。 | 在获准的大规模采集项目中，可借鉴其“URL 去重、任务队列、失败分类和有限重试”的工程组织。 | **不引入正式评测执行路径。**它也有原生 HTTP 爬虫及代理相关能力；这些既不需要、也不能替代赛事的“官方 CDP + Playwright”。若以后做赛事外的授权数据采集，可单独评估。 |
| [Scrapy](https://github.com/scrapy/scrapy) | **高（Python HTTP 爬虫）**：由 Zyte 和社区维护，仓库为 [BSD-3-Clause](https://github.com/scrapy/scrapy/blob/master/LICENSE)，仍持续发布。[官方仓库](https://github.com/scrapy/scrapy)。 | `AutoThrottle` 会按响应延迟调整速率，且不会因非 200 响应而降低延迟；`CookiesMiddleware` 可以按 cookie jar 保持正常站点会话。[AutoThrottle 文档](https://docs.scrapy.org/en/master/topics/autothrottle.html)；[CookiesMiddleware 文档](https://docs.scrapy.org/en/latest/topics/downloader-middleware.html)。 | **不作为本 Challenge 的浏览器驱动或直接取数客户端。**可把其限速/去重/错误退避原则移植到本项目的 Playwright 调度层；不要在评测中用 Scrapy/requests 绕开规定的浏览器轨迹。 |
| [Cloudflare Playwright fork](https://github.com/cloudflare/playwright) + Cloudflare Browser Run（原 Browser Rendering） | Cloudflare 官方维护的 Playwright fork，仓库为 [Apache-2.0](https://github.com/cloudflare/playwright/blob/main/LICENSE)。官方文档说明它专为 Workers/Browser Run 兼容，当前 `@cloudflare/playwright` 为 1.3.0、基于 Playwright 1.58.2。[官方文档](https://developers.cloudflare.com/browser-run/playwright/)。Browser Run 本身是**托管服务**，不是一个开源 Challenge 解锁器。 | 适合账户所有者在自己的 Cloudflare 账户中部署 Worker，或调用 Browser Run 的受权限控制的渲染/提取 API；API 要求 Cloudflare API Token 和 `Browser Rendering Write` 权限。[Browser Rendering API](https://developers.cloudflare.com/api/resources/browser_rendering/subresources/json/methods/create/)。 | **不用于正式评测。**它会改用 Cloudflare 管理的外部浏览器/运行环境，不能替代赛事分配的 CDP 浏览器。即使在赛事外，也不应宣称它能取得未授权站点或绕过 Challenge。 |
| [cloudflared](https://github.com/cloudflare/cloudflared) | **高（Cloudflare Access/Tunnel 客户端）**：Cloudflare 官方仓库，Apache-2.0，持续发布。[仓库与许可证](https://github.com/cloudflare/cloudflared)。 | 仅当目标应用的管理员已配置 Cloudflare Access 时，终端用户可完成自己的 IdP 登录；无人值守自动化则由管理员创建 Service Token，并以 Service Auth policy 明确授予访问权。[Cloudflare 的 agent 认证说明](https://developers.cloudflare.com/cloudflare-one/access-controls/authenticate-agents/)；[Service Token 文档](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/)。 | **仅适用于本方/已授权的 Access 应用，且不用于赛事。**它解决的是身份认证，不是公共网站 WAF Challenge；第三方站点不可能由客户端自行创建或索取其 Access token。 |

### 选型说明

- “成熟”按维护主体、可核验的发布历史、明确许可证和官方文档完整度判断，不把 GitHub star 数当作安全性或授权的证明。
- Playwright 的 `storage_state` 只适用于网站允许自动化、且账号所有者明确授权的会话。官方特别提醒其中可含敏感 cookie 和 headers，不能提交到 Git；这也意味着不能把任意 `cf_clearance` 或他人的会话材料当成项目配置。[Playwright 认证安全提示](https://playwright.dev/python/docs/auth)。
- Cloudflare Browser Run 及其 fork 是可信的**平台/客户端组合**，但不是纯开源替代品：实际浏览器由 Cloudflare 服务提供，使用它需要本方 Cloudflare API 凭据与相应权限。它能让本方应用更可靠地渲染页面，不能赋予对受保护第三方站点的访问权。

## WebRetriever 的可落地路径

### 正式评测：只做下面四项

1. 保持 `connect_over_cdp` / 赛事传入 CDP URL 和标准 Playwright，不更换浏览器、不新增 HTTP 爬虫或外部浏览器服务。
2. 在既有网络审计模块中，以域名和请求 URL 去重：只保存浏览器**实际收到的成功响应**，为内容设置大小上限与题目级生命周期；从中解析页面已加载的数据，而非猜测私有接口或枚举端点。
3. 为每个域名设低并发、有限等待与失败熔断。检测到 Challenge 时，先对当前可见验证控件做一次可审计交互并等待其结果；对 `Access Denied`、429/503，或验证控件已交互但页面无变化的重复失败，截图和记录 URL/状态后停止无效点击；这同时节约 100 步上限并减少对目标站的压力。
4. 只有在题目起始站点可见地链接到发布者自己的 API、下载文件或数据门户时，才用**同一个 Playwright 页面**导航并读取该第一方资源；不使用搜索引擎、代理或站外答案服务。

这四项与本仓库已有的[规则与合规调研](webretriever-rules-and-compliance.md)一致，且直接针对任务 69 的“已有成功网络响应但没有尽快结构化提取”这一失效模式。

### 赛事外、且取得书面授权时

- **受 Cloudflare Access 保护的本方应用**：请管理员建立最小权限的 Service Auth policy，发放可轮换/可撤销的 Service Token；或让实际用户按 IdP 正常登录。Cloudflare 明确区分两者：`cloudflared` 用于有用户参与的登录，Service Token 用于无人值守工作流。[官方指引](https://developers.cloudflare.com/cloudflare-one/access-controls/authenticate-agents/)。
- **本方 Cloudflare 账户上的渲染工作负载**：可以评估 Browser Run / `@cloudflare/playwright`；预算、数据处理和目标站条款另行审批。此路径是部署选择，不是反检测措施。
- **第三方数据站点**：优先联系发布者申请 API key、数据下载或自动化白名单；若没有授权，Challenge 失败应被报告/记录而不是“破解”。

## 明确排除项

下列类别不进入 WebRetriever 的正式镜像，也不在本调研中给出实现细节：

| 排除项 | 排除原因 |
| --- | --- |
| stealth / anti-detect / 指纹注入、修改浏览器可检测性、补丁版自动化内核 | 目标是规避安全判定；公开赛事规则也未明确授权此类修改。 |
| CAPTCHA/Turnstile 的外部自动解题、人工作坊或第三方“通关”服务 | 将安全校验外包或规避，不是合法的站点授权接入；不包括赛事明确允许的、在官方浏览器内对可见控件进行 Playwright 交互。 |
| 住宅/轮换代理、出口 IP 切换、会话/`cf_clearance` 买卖或复用 | 改变访问身份或使用不属于本方的授权材料，且不符合赛事提供固定可审计浏览器的前提。 |
| 以 Crawlee/Scrapy/requests/curl 直接访问目标内容 | 它们在获授权的普通采集项目中可以有正当用途，但 Protocol III 要求所有浏览器交互走 Playwright。 |
| 在正式评测中改接 Browser Run、cloudflared 或另起本地 Chromium | 它们会把执行路径移出官方分配的 CDP 浏览器；适用场景是自有/授权的系统，不是这项赛事。 |

## 来源范围

本文优先使用项目的官方 GitHub 仓库、源码许可证和 Cloudflare/Playwright/Scrapy 官方文档；没有采用论坛帖、营销文章或声称“可过 Cloudflare”的工具页面作为证据。Cloudflare 与目标网站的服务条款、赛事主办方后续解释及书面授权优先于本调研。
