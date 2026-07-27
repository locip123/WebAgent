# WebRetriever Challenge：网络拦截与反检测方案的合规边界

核验日期：2026-07-26。本文只判断**当前公开的官方比赛规则**对正式评测的约束；不把规则未写到的事项推定为允许，也不替代目标网站的服务条款、法律或主办方的书面答复。

## 可直接据此执行的结论

正式评测应继续使用**官方传入的 CDP 云端浏览器 + 标准 Playwright**。Browser Use 这类框架可以保留，但所有实际浏览器交互仍必须由 Playwright 完成。外部搜索引擎不可用；不要以代理、定制浏览器、CAPTCHA 解题服务、伪造/注入指纹或非 Playwright CDP 客户端作为正式评测的默认解法。

当前公开规则没有明文列出「住宅代理、浏览器代理、反检测/stealth、指纹伪装、CAPTCHA 绕过、目标站登录」的许可或禁止条款。因此这些不是已获准能力；若确有必要，应在合入正式提交前取得主办方的书面确认。官方说明主办方会进行轨迹验证，且由选手代码保存截图、动作与 XHR/Fetch 记录，故这类行为也不应假设不可见。[官方 Guide：搜索引擎与轨迹验证](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#faq)；[官方 Guide：采集的轨迹与网络记录](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#faq)。

## 官方规则：已明确的边界

### 关键原文与适用范围

> “评测环境通过 CDP 提供云端浏览器，Agent 必须基于 Playwright 进行所有浏览器交互操作。” [官方 Guide 原文](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#prepare)；[官方源码第 178–181 行](https://github.com/Mininglamp-AI/WebRetriever_Challenge/blob/main/docs/guide/index.html#L178-L181)

> “Agent 实现：自行设计导航与信息提取逻辑，模型、框架、工具均不限（禁止使用搜索引擎）。” 这句位于**备赛调试**栏；正式评测栏同时规定官方云端沙箱和自动传入的 CDP URL。因此“工具不限”不能覆盖正式环境的 Playwright-only 硬约束。 [官方源码第 235–273 行](https://github.com/Mininglamp-AI/WebRetriever_Challenge/blob/main/docs/guide/index.html#L235-L273)

> “不允许。Protocol III 考察从指定网页出发的导航与信息提取能力。赛后将进行操作轨迹验证……” [官方 Guide 原文](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#faq)；[官方源码第 382–387 行](https://github.com/Mininglamp-AI/WebRetriever_Challenge/blob/main/docs/guide/index.html#L382-L387)

| 事项 | 规则结论 | 一手来源 |
| --- | --- | --- |
| 正式浏览器自动化 | **必须**通过 Playwright 完成全部浏览器交互；评测浏览器是官方经 CDP 提供的云端浏览器。 | [Guide「如何备赛」](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#prepare)；[对应官方源码](https://github.com/Mininglamp-AI/WebRetriever_Challenge/blob/main/docs/guide/index.html#L178-L181) |
| 浏览器来源 | 正式评测由官方自动传入 CDP URL、提供并分配浏览器沙箱；不是由选手另起浏览器来替换。 | [Guide FAQ](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#faq)；[对应官方源码](https://github.com/Mininglamp-AI/WebRetriever_Challenge/blob/main/docs/guide/index.html#L401-L405) |
| 第三方 Agent 框架 | **允许** Browser Use 等框架，前提是接受 CDP URL 与任务输入，并按规定格式输出；这不覆盖上面「所有浏览器交互必须经 Playwright」的硬约束。 | [Guide FAQ](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#faq)；[对应官方源码](https://github.com/Mininglamp-AI/WebRetriever_Challenge/blob/main/docs/guide/index.html#L409-L414) |
| 外部搜索引擎 | **禁止**。Protocol III 要求从指定网页出发；赛后将验证轨迹，确认结果来自浏览器操作而非搜索引擎。 | [Guide FAQ](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#faq)；[对应官方源码](https://github.com/Mininglamp-AI/WebRetriever_Challenge/blob/main/docs/guide/index.html#L382-L387) |
| 限额与重试 | 最多 8 个任务并发、每题最多 100 步、每题无重试（失败为 0 分）。因此降并发和一次任务内有限退避可行，但不能把失败题作为新任务重跑。 | [Guide FAQ](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#faq)；[对应官方源码](https://github.com/Mininglamp-AI/WebRetriever_Challenge/blob/main/docs/guide/index.html#L376-L380) |
| 操作可审计性 | 截图、动作记录和 XHR/Fetch 请求由参赛代码保存到输出目录，评测系统会读取。 | [Guide FAQ](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#faq)；[对应官方源码](https://github.com/Mininglamp-AI/WebRetriever_Challenge/blob/main/docs/guide/index.html#L427-L432) |
| 「代理」一词的唯一明示用法 | 官方仅明确禁止用**模型 API 中转**来调用被禁用的闭源模型版本；该句不能被扩展解释成“浏览器代理已获许可”或“一律被禁止”。 | [Guide 模型规则](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#faq)；[对应官方源码](https://github.com/Mininglamp-AI/WebRetriever_Challenge/blob/main/docs/guide/index.html#L356-L368) |

### 当前公开文本的空白项

我逐项审阅了公开 Guide 及其官方赛事站源码。除上述模型 API 中转条款外，未找到对浏览器代理、住宅/轮换 IP、浏览器指纹、stealth、反爬绕过、Cloudflare、CAPTCHA 或目标站账号登录的具体比赛条款。该结果只能说明**公开规则尚未授权或说明这些做法**，不能推出许可；正式规则仍以公开 Guide 的更新和主办方解释为准。[官方 Guide](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/)；[官方赛事站源码](https://github.com/Mininglamp-AI/WebRetriever_Challenge/tree/main/docs)。

## 用户列出的项目：正式评测的保守分类

以下分类针对“是否值得作为正式比赛提交的默认方案”，不是对各项目技术能力或许可证的评价。

| 分类 | 项目/做法 | 原因与建议 | 规则依据 |
| --- | --- | --- | --- |
| 推荐 | **Browser Use + 标准 Playwright + 官方 CDP URL** | 与「第三方框架不限」及「全部浏览器交互经 Playwright」同时一致。应作为唯一默认运行路径。 | [框架 FAQ](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#faq)；[Playwright 要求](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#prepare) |
| 仅本地诊断 | `rebrowser/rebrowser-bot-detector` | 它可用于备赛期识别本地自动化暴露面，不替换正式浏览器、不参与正式执行路径。它不能证明任何绕过技术已获比赛许可。 | [备赛/正式环境的区分](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#faq) |
| 需书面确认 | Patchright、Rebrowser Playwright / patches | 虽与 Playwright 生态相关，但其目标是修改自动化可检测性；公开规则没有说明“补丁版 Playwright”是否仍满足强制的 Playwright 约束。未获确认前，不作为正式提交依赖。 | [Playwright 要求](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#prepare) |
| 需书面确认 | `playwright-stealth` 两个 Python 版本 | 它们仍通过 Playwright 调用浏览器，但其 stealth 注入不在公开规则的明确许可范围内。只可用于本地兼容性实验，正式使用前询问主办方。 | [Playwright 要求](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#prepare) |
| 需书面确认 | BrowserForge、Apify Fingerprint Suite | 生成器本身不是浏览器交互；但将生成的请求头/指纹注入官方已分配浏览器会改变其身份特征，公开规则没有授权。不要把它们作为正式反封锁手段。 | [官方浏览器与轨迹](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#faq) |
| 不推荐用于正式评测 | Zendriver、Nodriver | 这是以非 Playwright 的 CDP 自动化框架执行浏览器交互，与“所有浏览器交互必须通过 Playwright”的明确要求不相容。 | [Playwright 要求](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#prepare) |
| 不推荐用于正式评测 | Puppeteer Extra Stealth | Puppeteer 不是 Playwright；即便附带 stealth 插件也不满足明确的浏览器交互方式。 | [Playwright 要求](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#prepare) |
| 不推荐用于正式评测 | CloakBrowser、BotBrowser，以及自带 profile bundle 的定制 Chromium | 其设计前提是替换/定制浏览器或 profile；而正式评测已由官方提供和分配云端浏览器沙箱，选手不应以另一个浏览器替代。 | [官方浏览器](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#faq) |
| 不推荐、先问再说 | 浏览器/住宅/轮换代理、外部 CAPTCHA 解题服务、绕过 Cloudflare/登录限制 | 当前规则没有明示许可，且会影响官方浏览器的网络身份或把交互移出可审计的 Playwright 路径。不要把“未禁止”当作允许。 | [官方浏览器与轨迹](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#faq) |

对于任何“需书面确认”项，建议向赛事空间或 [官方联系页](https://mininglamp-ai.github.io/WebRetriever_Challenge/#contact)提交一个可二选一回答的问题：**“在不更换官方 CDP 浏览器、不使用外部搜索引擎的条件下，是否允许在 Playwright 内使用 `<具体库/具体配置>`？”** 同时附上会保留完整轨迹、不会使用外部 CAPTCHA 人工/解题服务的说明。

## 三个失败任务的事实核对（不含标准答案）

本节仅使用本地任务文件和已有轨迹诊断失败形态；不把公开数据中的 `answer` 传给 Agent 或写入本文。

| 任务 | 任务要求与观察到的失败 | 合规优先的改进方向 |
| --- | --- | --- |
| 37：SEC / NVIDIA 首次 10-K | [任务数据](../../data/data/protocol3.json)要求从 `sec.gov` 查询 NVIDIA 首次 10-K 日期；[轨迹](../../outputs/protocol3_6/37_FAIL/result.json)记录的是 `Request Rate Threshold Exceeded`，并已等待累计 100 秒。它首先是**速率/会话限流**信号，不能仅凭此断言为浏览器指纹问题。 | 将 SEC 相关访问按域名限速；避免同一题内连续打开多个静态/公司数据端点；降低全局并发而非使用代理或 stealth。所有动作仍留在该题、Playwright 与 100 步上限内。 |
| 31：美国护照申请量 | [任务数据](../../data/data/protocol3.json)要求从 `travel.state.gov` 找出指定财年范围的最高值；[轨迹](../../outputs/protocol3_5/31_fail/result.json)显示 Cloudflare 拦截，且只有一次 5 秒等待。 | 先把“拦截页”识别为明确状态，采用一次任务内、有限且低频的正常等待/站内导航策略；若仍未放行则如实失败并保留证据。不要接入外部 CAPTCHA 服务、代理或搜索引擎。 |
| 36：BLS Information 就业人数 | [任务数据](../../data/data/protocol3.json)要求从 `data.bls.gov` 查询一个 CES 值；[轨迹](../../outputs/protocol3_5/36_fail/result.json)显示 BLS `Access Denied` / bot activity prohibited，多个官方端点也被拒绝。 | 避免端点枚举和短时间内的连续直链访问；优先按起始页面的正常站内操作链，并在遇到拦截时收集证据、停止无效尝试。不能由该现象推出“允许修改指纹”。 |

三题的任务文字均没有要求登录、提供账号凭据或完成 CAPTCHA。因此在当前公开信息下，把“登录/CAPTCHA 绕过”加入正式 Agent 并不是解决这些题目的合规默认路径。

## 更优先的第一方公开数据恢复（仍通过 Playwright）

以下是“使用目标发布者自己公开的数据服务”，不是改变浏览器指纹、代理出口或绕过安全校验。正式任务中仍应由官方传入的浏览器以 Playwright `navigate` 打开，读取实际返回内容并把该页面/响应写入轨迹；若接口同样拒绝，则停止，而不是改用直接 HTTP 客户端或规避手段。

| 任务 | 可验证的第一方路径 | 合规使用方式 |
| --- | --- | --- |
| 37：SEC | SEC 说明 `data.sec.gov/submissions/CIK##########.json` 提供公司提交历史，且不要求 API key；CIK 必须是 10 位、含前导零。[SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) | 先以一次普通的 SEC 页面/已知第一方目录取得并核验 CIK，再在同一浏览器中打开对应 submissions JSON，筛选并验证最早的 10-K/10-K 变体记录。必须使用真实组织名和联系邮箱作为 User-Agent，并遵守 SEC 每个用户最多 10 请求/秒的 Fair Access 上限；不要因限流更换 IP 或伪装身份。[SEC Developer Resources](https://www.sec.gov/about/developer-resources)；[SEC Webmaster FAQ](https://www.sec.gov/about/webmaster-frequently-asked-questions) |
| 36：BLS | BLS 的 Public Data API 对单一时间序列使用 GET；官方文档公开了 v2 的单序列签名。当前项目已为本题登记 `CES5000000001` 的 Playwright-only fallback，但本次失败轨迹没有尝试它。[BLS API 签名](https://www.bls.gov/developers/api_signature_v2.htm)；[本项目 prompt](../../browser_use/webretriever/prompts.py) | 在普通 BLS HTML/下载页出现拒绝后，只打开一次目标化单序列 API，核验返回的 series ID、年份和月份后再提取值。BLS 允许非过量 robot，但会阻断无联系人信息或过量/恶意访问；可使用真实、稳定的联系身份与严格低频率，绝不设法规避其安全措施。[BLS Terms of Use](https://www.bls.gov/bls/blsterms.htm) |
| 31：国务院护照统计 | `https://cadataapi.state.gov/` 的页面自称 “DoS - CA API”，并可见列出 “All Applications by FiscalYear” 的第一方端点；它属于 `state.gov`，而非搜索引擎或第三方聚合站。[Department of State CA API](https://cadataapi.state.gov/) | 这是 Cloudflare 阻断 `travel.state.gov` 时可在备赛环境验证的候选回退：先打开该 API 根页，再点击其可见的 “All Applications by FiscalYear” 链接，以保留来源轨迹并计算范围内最大值。若该服务在官方云端浏览器中也返回拒绝，直接保留证据并向组委会报告；不得改用代理、CAPTCHA 解题或外部搜索。 |

这些回退路径应实现为站点级、一次性、可审计的策略，而不是把答案或 CIK/数值硬编码进 Agent。首要测试是官方的 smoke/评测环境能否以**未伪装的标准 Playwright**访问它们；本地成功不能证明云端出口同样可访问。

## 推荐的实现优先级

1. 保持官方 CDP 浏览器与标准 Playwright；Browser Use 仅作为上层编排框架。
2. 加入站点级低并发、请求节流、重复 URL 去重、对 `Access Denied` / Cloudflare / 限流页的早期识别，并记录证据；这既保留 Playwright 轨迹，也不触碰身份伪装。
3. 在同一题内使用有限退避和正常站内页面路径，严格计入 100 步；不要把它实现成失败题重跑。
4. 外部搜索引擎始终禁用；需要定位信息时只能从题目指定站点的可见页面、站内功能和经浏览器产生的网络记录中取得。
5. 只有在主办方明确书面允许后，才评估任何 stealth、指纹注入、代理或 CAPTCHA/登录自动化方案；否则不纳入正式镜像。
