# Playwright stealth 与浏览器指纹开源方案

核验日期：2026-08-04

## 结论

针对本仓库的运行方式——Python、Playwright 1.61、由赛事传入一个已经启动的 Chromium
CDP URL——最值得实测的是 **Patchright**。第二候选是 **Rebrowser 前移补丁驱动**：其
Python 包当前公开版本停在 Playwright 1.52，与本项目 1.61 不匹配，因此本仓库只在
显式实验路径中对已知 1.61 Node driver 哈希应用可恢复补丁。传统
`playwright-stealth` 和指纹注入器可以作用于页面或新 context，却主要依赖 JavaScript
覆盖；若覆盖值与真实浏览器、UA-CH、GPU、操作系统或网络指纹不一致，可能增加而不是
减少风险信号。

Camoufox、CloakBrowser、nodriver 和 undetected-chromedriver 都是有效的开源研究方向，
但它们依赖自行启动定制浏览器或使用非 Playwright 控制面，不能替换赛事已经分配的
Chromium。公开赛事规则没有点名禁止 Patchright 或指纹工具，但要求所有浏览器交互通过
Playwright、正式评测接收官方 CDP URL，并保存可审计轨迹；在正式使用前应取得组委会
书面确认。[WebRetriever Challenge 评测指南](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/)

## 候选项目

| 项目 | 主要机制 | 既有 Chromium CDP | 本项目适配性 |
| --- | --- | --- | --- |
| [Patchright Python](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) | 修改 Playwright driver，规避 `Runtime.enable`、Console、默认启动参数等泄漏；保留 Playwright API | **可连接；客户端补丁可部分生效**，但无法改变官方浏览器已有启动参数 | **首选 A/B 候选**；Python 与 API 迁移成本最低，需规则确认 |
| [rebrowser-patches](https://github.com/rebrowser/rebrowser-patches) / [rebrowser-playwright-python](https://github.com/rebrowser/rebrowser-playwright-python) | 修补 Playwright/Puppeteer 源码中的 `Runtime.enable`、utility world、sourceURL 等 CDP 痕迹 | **可连接；客户端补丁可生效** | [PyPI 最新公开版为 1.52.0](https://pypi.org/project/rebrowser-playwright/)，故本仓库维护仅面向已知 Playwright 1.61 driver 哈希的、可恢复的前移补丁；只可作为显式对照实验候选 |
| [playwright-stealth](https://github.com/AtuboDad/playwright_stealth) | 将 puppeteer-extra 的 navigator、plugins、WebGL 等 evasions 作为 init script 注入页面 | **可以**，必须在目标文档脚本运行前注入 | 接入容易但项目最后一次仓库提交为 2023-09；只适合小范围实验，不应默认全开 |
| [puppeteer-extra-plugin-stealth](https://github.com/berstend/puppeteer-extra/tree/master/packages/puppeteer-extra-plugin-stealth) / `playwright-extra` | Node 插件体系，多组 JavaScript evasions | Playwright-extra/Puppeteer 可以接入 CDP | Node 控制面与当前 Python runner 不匹配；原插件自己也承认不可能消除全部检测方式 |
| [Apify fingerprint-suite](https://github.com/apify/fingerprint-suite) | 贝叶斯生成相互关联的 headers、UA、screen、navigator、WebGL 等并注入 Playwright context | **部分可用**，通常创建新的 injected context | 项目活跃，但以 Node 为主；不能伪装 TLS、IP 或官方浏览器启动状态 |
| [BrowserForge](https://github.com/daijro/browserforge) | Apify fingerprint-suite 的 Python 重实现，生成 headers/fingerprint | 生成器本身不接管 CDP；旧 Playwright injector 已标记 deprecated | 可做一致性测试数据源，不应作为独立 stealth 方案；官方建议改用 Camoufox |
| [Scrapling](https://github.com/D4Vinci/Scrapling) | `StealthyFetcher` 等上层抓取接口，浏览器 stealth 底层使用 Patchright | 主要设计为由 fetcher 自行管理浏览器 | 不是新的底层机制；直接接入会绕过本项目现有 page/context、轨迹和动作管理 |
| [Camoufox](https://github.com/daijro/camoufox) | 定制 Firefox/Juggler，在浏览器内部隔离 Playwright 并实现指纹伪装 | **不能接管赛事 Chromium CDP** | 通用场景能力强，但正式比赛基本不适用；官方也披露过维护空档与指纹一致性退化 |
| [CloakBrowser](https://github.com/CloakHQ/CloakBrowser) | 定制 Chromium，在 C++ 层修改 canvas、WebGL、GPU、UA、CDP 等 | 必须运行它自己的 Chromium build | 新兴且项目自测结果很强，但无法追溯修改官方已启动浏览器，成熟度仍需观察 |
| [nodriver](https://github.com/ultrafunkamsterdam/nodriver) | 不用 WebDriver/Selenium，直接使用 CDP；支持连接已运行 Chrome | **可以** | 不属于 Playwright，正式评测规则风险高 |
| [undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver) | 修改 Selenium/ChromeDriver 与启动行为 | 通常自行启动或接管 Chrome | 不属于 Playwright；作者已将 nodriver 定位为后继项目 |

## 技术层级

### 1. Driver/CDP 泄漏补丁

Patchright 和 rebrowser-patches 针对自动化客户端本身的可观察行为，而不是随机伪造大量
设备属性。Patchright 官方列出的核心补丁包括避免 `Runtime.enable`、关闭 Console API
以及修改默认启动参数；rebrowser 也把 `Runtime.enable` 视为主要泄漏，并提供 isolated
context 等修复模式。[Patchright patches](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python#patches)；
[rebrowser available patches](https://github.com/rebrowser/rebrowser-patches#available-patches)

连接既有 CDP 浏览器时，driver/runtime 类修复仍可能生效；浏览器二进制、headless 模式、
扩展和命令行参数类修复则无法生效。因此必须在同一个官方 CDP、同一出口 IP 上进行
Playwright/Patchright A/B，而不能用本地自行启动的浏览器结果代替。

### 2. JavaScript stealth 脚本

playwright-stealth 与 puppeteer-extra-plugin-stealth 主要通过 `add_init_script`/evaluate-on-new-document
覆盖 `navigator.webdriver`、plugins、languages、WebGL 等页面可见属性。优点是能附着到
普通 Playwright 页面；缺点是 iframe、worker、原型描述符、原生函数文本和跨层一致性都
可能暴露覆盖行为。两个常见 Python 仓库的最近主分支提交分别停在
[2023-09](https://github.com/AtuboDad/playwright_stealth/commits/main/) 和
[2022-06](https://github.com/Granitosaurus/playwright-stealth/commits/main/)，不宜把公开 bot test
通过率当作现代 Cloudflare 的保证。

### 3. 指纹生成与注入

Apify fingerprint-suite 会共同生成 HTTP headers 与浏览器 JS API 指纹，避免独立随机 UA、
屏幕或平台。它仍主要通过新 Playwright context 注入，不能改变 TLS、出口 IP、真实 GPU、
浏览器启动参数等其他层。[Apify fingerprint-suite 官方仓库](https://github.com/apify/fingerprint-suite)

BrowserForge 是 Python 版本的生成器，但其官方 README 已把 Playwright fingerprint injection
标记为 deprecated，并建议使用 Camoufox。[BrowserForge 官方仓库](https://github.com/daijro/browserforge)

Cloudflare 官方明确列出两类不受支持的配置：修改 User-Agent、Canvas 或 WebGL 的扩展；
以及挑战与 solve 请求来自不同 IP 的客户端。后者会直接导致 challenge loop。因此指纹
注入的目标应是“跨层一致”，不是随机字段越多越好。
[Cloudflare Challenge limitations](https://developers.cloudflare.com/cloudflare-challenges/concepts/how-challenges-work/#limitations)

### 4. 定制浏览器内核

Camoufox、CloakBrowser 等把修复放进 Firefox/Chromium 内核，理论上比页面 JavaScript
覆盖更难被直接观察。代价是必须下载并启动项目自己的浏览器二进制，无法作用到赛事已经
启动的官方 Chromium，所以适合赛事外的授权自动化研究，不适合作为本次正式评测后端。

## 对本仓库的实验顺序

1. 先在标准 Playwright 基线中移除 `HeadlessChrome -> Chrome` 的 UA 覆盖，并在 Cloudflare
   验证期间暂停额外 `Runtime.evaluate`、DOMSnapshot 和多 CDP session 观察。
2. 用同一个 CDP URL 和相同任务做标准 Playwright 与 Patchright A/B；记录挑战出现、勾选、
   `Verifying`、页面重置和 `cf_clearance` 时间点。
3. 只有 Patchright 的 driver 补丁没有改善时，才测试 playwright-stealth 的单项 evasion；
   不启用 UA、platform、WebGL、screen 等整套随机覆盖。
4. 使用 `--rebrowser-experiment` 在一个 CDP 端点、一个顺序轮次中对照标准 Playwright 和
   Rebrowser；前移补丁必须先通过 `webretriever-rebrowser-smoke`，并记录实际
   `REBROWSER_PATCHES_RUNTIME_FIX_MODE`。该模式下不使用 `page.set_content()`，因为它依赖
   被 Runtime.Enable 缓解关闭的 console 事件；正常任务通过网页导航，不受此限制。
5. Camoufox、CloakBrowser、nodriver、undetected-chromedriver 不进入正式比赛实验矩阵。

## 边界与证据强度

- 项目 README 中“通过 Cloudflare/所有 bot tests”属于项目方自测声明，不等同于对 LOC、
  官方比赛沙箱或未来 Cloudflare 版本的保证。
- stealth 只能减少浏览器/自动化信号，不能修复出口 IP 信誉、速率限制、站点账号状态或
  网络不稳定。
- 公开规则对 Patchright 没有明确文字结论；技术可行性与比赛许可应分开验证。
