# WebRetriever：Playwright 验证交互与开源项目选型

核验日期：2026-08-04

## 结论

本项目不需要用 Patchright 替换 Playwright。当前最有效的改进是，在现有标准
Playwright runtime 上增加一个优先于普通 Agent 决策的**验证状态机**：发现验证页后，
先点击页面中可见的验证控件，然后在验证处理中持续等待；成功后沿用同一个
BrowserContext、cookie 和标签页继续任务。只有 DOM 无法定位控件时，才使用截图视觉
定位模型返回坐标，实际点击和拖拽仍由 Playwright 完成。

公开规则要求正式评测使用官方传入的 CDP 云端浏览器，并由参赛代码保存截图、动作和
网络轨迹；第三方 Agent 框架可以使用，但外部搜索引擎禁止使用。参赛模型还受版本和
审计要求约束。[官方评测指南](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/)

这里讨论的只是比赛明确纳入考察范围的、官方浏览器中**可见且可审计**的验证交互；
不建议更换访问身份、注入验证 token、伪造指纹、接入外部代解或绕过网站安全策略。

## 本地轨迹已经证明问题不在浏览器底座

任务 69 的[结果轨迹](../../outputs/protocol3_10/69_714f9fbc74a94c8ebbacfc72f223c85f/result.json)
显示：

1. 第 24 步进入 Cloudflare 页面，但 Agent 先花了二十余步反复检索网络响应。
2. 第 47 步，现有语义元素收集已经找到验证控件，标准 Playwright 成功执行
   `click(element_id=0)`。
3. [第 48 步截图](../../outputs/protocol3_10/69_714f9fbc74a94c8ebbacfc72f223c85f/trajectory/48.png)
   中复选框已经打勾。
4. [第 49 步截图](../../outputs/protocol3_10/69_714f9fbc74a94c8ebbacfc72f223c85f/trajectory/49.png)
   显示 “Verifying you are human. This may take a few seconds.”，但 Agent 随即执行
   `back`，主动打断了验证。

因此当前首要缺陷是验证页面没有抢占普通规划，以及没有区分“需要点击”和“正在处理”
两个状态。现有 runtime 已具备原始截图、跨 frame 元素观察、语义点击、坐标点击、分段
拖拽、同一 CDP context 复用和 XHR/Fetch 捕获，完成该状态机不需要新增浏览器驱动。

## 相关开源项目

| 项目 | 可借鉴能力 | 对正式评测的适配结论 |
| --- | --- | --- |
| [Browser Use](https://github.com/browser-use/browser-use) | 仓库中的 `CaptchaWatchdog` 在解题期间暂停 Agent step loop，并记录 success/failed/timeout；项目为 MIT。 | **借鉴 watchdog/暂停语义，不直接复用 solver。** 当前实现依赖 Browser Use Cloud 浏览器代理发出的私有 `BrowserUse.captchaSolverStarted/Finished` CDP 事件，官方比赛浏览器不会发这些事件。本仓库已有对应[源码](../../browser_use/browser/watchdogs/captcha_watchdog.py)。 |
| [Midscene.js](https://github.com/web-infra-dev/midscene) | 纯视觉 UI 定位，MIT；官方文档展示了 `chromium.connectOverCDP()` 后把现有 Playwright `Page` 传给 `PlaywrightAgent`。[远程 Playwright 示例](https://midscenejs.com/integrate-with-playwright#connect-midscene-agent-to-a-remote-playwright-browser) | **最值得参考的上层设计。** 它证明“视觉决策、Playwright 执行动作、复用既有 CDP Page”可行；但主实现为 TypeScript，引入整套 Node 控制面会与当前 Python runner、轨迹和步数管理重复，宜借鉴设计而不是整体替换。 |
| [UI-TARS](https://github.com/bytedance/UI-TARS) | Apache-2.0；开源 GUI 模型可从截图生成点击、拖拽等坐标动作，仓库也推荐用 Midscene 做 Web 自动化。 | **适合作为可选的截图 grounding 模型。** 只让它输出控件坐标，再调用当前 `click_xy`/`drag`；不要运行其桌面 operator 或 PyAutoGUI 动作。使用自部署模型时须遵守比赛模型日期、版本和权重审计规则。 |
| [UGround](https://github.com/OSU-NLP-Group/UGround) | MIT；专门将“控件描述 + 截图”映射到点坐标，官方示例输出 `[0,1000)` 归一化坐标。 | **最小视觉回退候选。** 比完整 Web Agent 更容易封装成无副作用的 locator 服务；它不连接浏览器，所有动作仍可留在 Playwright。缺点是需要单独部署模型并承担推理延迟。 |
| [OmniParser](https://github.com/microsoft/OmniParser) | 将截图解析成可交互区域、边界框和描述，适合 iframe、canvas 或 DOM 不透明控件。仓库代码为 CC-BY-4.0；官方说明不同检测/描述权重分别可能是 MIT 或旧版 AGPL。 | **第二视觉回退候选。** 可先离线解析当前 Playwright 截图，再把候选框交给主 VLM；安装和显存成本高于单纯依赖当前多模态模型，必须逐项核对所用权重许可证。 |
| [Skyvern](https://github.com/Skyvern-AI/skyvern) | Python、Playwright-compatible AI 自动化，支持连接现有 Chrome CDP；核心仓库 AGPL-3.0。 | **只参考其视觉交互与状态管理。** 官方仓库明确说明 anti-bot/CAPTCHA solver 属于其托管云能力，不在开源核心中；其云浏览器也不能替换赛事分配的 CDP 浏览器，整体引入过重。 |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | Apache-2.0；支持 `cdpEndpoint` 接入既有浏览器，提供 accessibility snapshot 和确定性动作。 | **不解决当前缺口。** 它偏 DOM/accessibility，截图不能直接作为动作依据，而且会复制本项目已有的 Playwright 控制、轨迹和 Agent tool loop。 |

### 不适用方案

- Patchright 与 Rebrowser 的 driver/CDP 修补只能降低客户端可观察信号，无法改变官方已经
  启动、只通过 CDP 交付的浏览器二进制、出口 IP 或启动参数；两者均应仅在同一 CDP 端点的
  可审计对照实验中使用。Camoufox、Scrapling 等路径通常还会自行启动浏览器或接管代理，
  不适合作为该评测的正式后端。
- Browser Use Cloud、Skyvern Cloud、Browserbase 等托管 CAPTCHA/stealth 能力绑定它们
  自己的浏览器和代理，不能安装到主办方的 CDP 沙箱中。
- 外部 CAPTCHA 代解服务、token 注入和人工打码平台会把验证移出可审计的 Playwright
  页面交互链。公开规则没有明确授权，不应作为提交方案。
- 正式评测是自动执行、无额外认证的云端任务；`page.pause()`、本地 headed 浏览器人工
  接管或远程桌面只适合备赛调试，不能作为正式得分路径。

## 推荐实现

新增一个很小的 `VerificationController`，放在 `ProtocolIIIAgent.run()` 每次
`observe()` 之后、普通模型调用之前：

```text
NONE
  └─ 检出验证页 → ACTION_REQUIRED
       ├─ 语义元素可用 → Playwright click(element_id)
       └─ DOM 不可用 → 截图 grounding → Playwright click_xy / drag
                              ↓
                         PROCESSING
                   ┌──────────┴──────────┐
              控件/验证页消失          明确失败或超时
                   ↓                    ↓
                PASSED               BLOCKED
```

建议细节：

1. 检测综合使用 title、当前页面文字、iframe/资源 URL 和截图分类；不要仅凭一个
   `challenge` 字符串判断。
2. `ACTION_REQUIRED` 中先使用已有语义元素；只有找不到控件时才调用窄任务视觉 grounding，
   并验证坐标位于 viewport 内。
3. 点击后若出现 checked、spinner、`Verifying...`、验证 token URL 等处理中信号，禁止
   `back`、reload、换标签页或重复点击；用 Playwright 每 2–5 秒重新观察，最多等待约
   20–30 秒。
4. 每个验证最多点击两次，且每次点击、等待和截图都进入正常步数与轨迹；同一画面无进展
   时熔断，避免耗尽每题 100 步。
5. 通过后继续复用官方提供的同一个 BrowserContext，不清 cookie、不新开浏览器。
6. 若最终阻塞，保留截图和状态；只分析该页此前由同一 Playwright 会话实际捕获的第一方
   响应，或走页面可见的第一方下载/API 链路，不另起 HTTP/stealth 客户端。

## 选型建议

第一阶段不要安装任何新项目，直接实现上述状态机并回放任务 69。当前 VLM 已经能看到
截图，而现有 DOM 收集也已经正确定位 Cloudflare 复选框。

只有当公开任务回放表明大量验证控件确实无法通过 DOM/现有 VLM 定位时，再做 A/B：

1. 首选 UGround，作为只返回坐标的独立 fallback；
2. 若还需要通用图标/区域解析，再评估 OmniParser；
3. Midscene 和 Browser Use watchdog 主要作为架构参考，不引入第二套浏览器控制器。
