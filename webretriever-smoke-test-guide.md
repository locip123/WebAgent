# WebRetriever Challenge 冒烟测试指南

> 适用范围：`hhhhhhalf/WR-047` 提交模板、当前 `/workspace/code/browser-use` 工程，以及腾讯云浏览器沙箱相关的本地/云端联调。
>
> 资料核对日期：2026-08-14。正式规则以后续官方 Guide、提交群通知和组委会书面答复为准。

## 先记住四条结论

1. **冒烟测试测的是提交链路是否能跑通，不等于最终成绩。** 代码必须被拉取、安装并启动，且至少能产生合规的任务结果；真正评分还要求导航到目标页面并让 `agent_answer` 与答案语义一致。
2. **比赛模板和当前工程的入口不同。** `WR-047/README.md` 规定评测方调用 `bash scripts/run.sh <task_file> <output_dir> <cdp_url_1> ...`；本工作区的工程约定是 `python run_webretriever.py`。本地开发按后者，提交模板必须保留前者的接口。
3. **正式评测使用官方分配的腾讯云 CDP 浏览器。** 不要自行创建浏览器、替换 CDP URL 或修改模板中的连接逻辑。聊天记录确认云端浏览器不是无头运行，默认窗口尺寸为 **1920 × 1080**；未确认具体是 Xvfb、X11 还是 Wayland。
4. **风控验证是题目环境的一部分。** Agent 应在官方浏览器内识别可见验证控件，进行一次正常的 Playwright 点击/等待并记录轨迹；不要使用外部搜索、代理切换、指纹伪装或第三方验证码代解作为默认方案。

## 1. 参赛仓库与本工作区的关系

### 1.1 `WR-047` 提交模板的硬约束

根据私有仓库 README 和提交群通知，正式提交至少应检查：

- `config.json`：模型 API 配置；
- `environment.yml`：运行时依赖；
- `scripts/run.sh`：固定入口，不改变位置参数含义；
- `src/`：Agent 逻辑。复杂模块可以放在其他目录，但 `src`、入口脚本和依赖文件必须完整；
- `main` 分支：评测系统只拉取 `main` 的最新 commit；
- 评测时必须接受任务文件、输出目录和数量可变的 CDP URL（最多 8 个）；
- 不要修改模板的 `init_playwright_context` 沙箱连接逻辑。

浏览器交互必须走 Playwright/CDP；模型、框架和答案提取实现可以自行设计。答案可以来自 DOM、截图 OCR、VLM 或浏览器实际捕获的网络响应，但最后必须写进 `result.json` 的 `agent_answer`。

### 1.2 当前工程的本地入口

本工作区遵循 `AGENTS.md`：

```bash
cd /workspace/code/browser-use
python run_webretriever.py
```

当前入口支持 TOML 配置和命令行覆盖，任务文件是 `data/data/protocol3.json`。它适合本地验证 Agent 和正式 CDP 运行；不能据此推断 `WR-047` 的 `scripts/run.sh` 接口可以删除或改名。

## 2. 冒烟测试前的准备

### 2.1 GitHub 权限和代码快照

队员首先要在 GitHub Notifications 接受 `hhhhhhalf` 的协作邀请，再确认能访问队伍私有仓库。建议在提交前执行：

```bash
cd /path/to/WR-047
gh auth status
gh repo view hhhhhhalf/WR-047
git branch --show-current
git status --short
git diff --check
```

确认当前分支是 `main`，工作区中没有未提交的必要改动。不要把私有仓库内容、模型密钥、CDP URL 或真实联系方式发到公共群组。

### 2.2 模型服务配置

比赛评测在云端运行，因此自部署模型必须从公网可达。自部署服务应能从另一台机器验证模型列表；闭源 API 或网关则确认完整的 OpenAI-compatible base URL、模型名和密钥可用。模板 README 的 API base 示例包含 `/v1`，并提醒最多可能同时承受 8 路请求。

不要把密钥写入 Git。推荐使用本地 `.env` 或 CI/评测平台的环境变量，并单独保留一份不含密钥的配置模板：

```dotenv
WEBRETRIEVER_API_BASE=https://model-gateway.example/v1
WEBRETRIEVER_MODEL=your-allowed-model
WEBRETRIEVER_API_KEY=***
```

在提交群要求的白名单表单中，只填写 Agent **直接访问的算法服务入口域名**：

- 直接调用官方 API：填写官方 API 域名；
- 通过网关：只填写网关域名，不必重复列出网关后面的模型供应商；
- 自部署：填写公网 IP 或域名；若服务在中国大陆以外，按通知要求填写域名；
- 使用多个入口：全部列出。

白名单表单应在正式提交前完成；目标网站域名、CDP 域名不属于这里要求收集的“算法服务入口域名”。

### 2.3 依赖和静态检查

按本机环境先激活 `Browser-Use`。如果当前 shell 尚未初始化 Conda，可先加载初始化脚本：

```bash
source /opt/miniconda/etc/profile.d/conda.sh  # 仅在 conda activate 报未初始化时需要
conda activate Browser-Use
python --version
```

对 `WR-047`：

```bash
test -f config.json
test -f environment.yml
test -x scripts/run.sh || test -f scripts/run.sh
bash -n scripts/run.sh
python -m compileall -q src
```

对当前工程：

```bash
python -m compileall -q browser_use run_webretriever.py
python -m pytest -q \
  tests/ci/test_webretriever_models.py \
  tests/ci/test_webretriever_browser.py \
  tests/ci/test_webretriever_agent.py \
  tests/ci/test_webretriever_network.py \
  tests/ci/test_webretriever_runner.py
```

测试失败时先修复语法、依赖和接口问题，再提交冒烟；不要把“能启动 Python”误当成“能在 8 个云端浏览器上运行”。

## 3. 先做不联网的任务文件校验

当前工程的 `--validate-only` 不启动浏览器、不调用模型，并且会丢弃任务文件中的公开标准答案，只把任务元数据交给 Agent。执行：

```bash
python run_webretriever.py \
  --input data/data/protocol3.json \
  --output outputs/validate \
  --validate-only
```

验收点：

- `task_count` 为 100；
- `task_indices` 连续覆盖 0–99；
- `ground_truth_exposed_to_agent` 为 `false`；
- 没有因 JSON 格式、重复 ID、非法起始 URL 或答案字段泄漏而报错。

这个步骤通过只说明输入文件安全、可加载，不说明模型质量和网站可访问性。

## 4. 本地浏览器冒烟

### 4.1 本地 CDP 通道（可选）

本地调试可以启动 Chrome 的远程调试端口，并检查 `/json/version`：

```bash
google-chrome \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/tmp/chrome-debug-profile" \
  --no-first-run \
  --no-sandbox \
  about:blank

curl -fsS http://127.0.0.1:9222/json/version | jq '{Browser, user_agent: .["User-Agent"]}'
```

如需比较有头浏览器对页面风控的影响，可在本机 Xvfb 中运行普通 headed Chrome，并把屏幕设置为 1920×1080。这个对照只用于本地诊断；不能替换正式评测的官方沙箱，也不能证明某种反检测技术获准。

### 4.2 当前工程：先跑单题，再跑前三题

为避免 `webretriever.toml` 中预设的 CDP 地址与 `--local-browser` 冲突，本地浏览器冒烟建议使用命令行和环境变量，不带 `--config`：

```bash
export WEBRETRIEVER_API_BASE='https://model-gateway.example/v1'
export WEBRETRIEVER_API_KEY='***'
export WEBRETRIEVER_MODEL='your-allowed-model'

python run_webretriever.py \
  --input data/data/protocol3.json \
  --output outputs/smoke_task0 \
  --local-browser \
  --headed \
  --task-index 0 \
  --max-concurrency 1 \
  --max-steps 100 \
  --model-timeout 180 \
  --task-timeout 600
```

单题成功后再执行：

```bash
python run_webretriever.py \
  --input data/data/protocol3.json \
  --output outputs/smoke_first3 \
  --local-browser \
  --headed \
  --task-index 0-2 \
  --max-concurrency 3 \
  --max-steps 100 \
  --model-timeout 180 \
  --task-timeout 900
```

如果使用配置文件，必须把 `local_browser = true`，并清空 `cdp_urls`；否则当前 runner 会拒绝同时启用本地浏览器和 CDP URL。

### 4.3 检查本地产物

```bash
jq . outputs/smoke_first3/logs/summary.json
find outputs/smoke_first3 -path '*/result.json' -exec \
  jq -r '[.task_idx, .status, (.agent_answer // "" | length), (.predict_length // -1)] | @tsv' {} \;
```

每道题至少检查：

- `result.json` 存在，`agent_answer` 非空；
- `predict_length <= 100`；
- `trajectory/` 有逐步 PNG；
- `capture.json` 存在且是合法 JSON；
- `logs/summary.json` 的任务数、Worker 数和状态计数合理；
- `agent_answer` 的证据来自当前浏览器页面、站内导航或浏览器捕获响应，而不是题目数据中的 `answer` 字段。

当前工程的 `SUCCESS` 主要表示“有非空答案和浏览器证据”；是否与标准答案语义一致仍应由离线评估或正式评测判断，不能只看 `SUCCESS`。

## 5. 模拟正式 CDP 运行

### 5.1 当前工程接入官方提供的 CDP URL

拿到评测方 CDP URL 后，不要把它打印到日志或截图。用环境变量保存，再先用 1 个 URL、1 道题验证：

```bash
export WEBRETRIEVER_CDP_URL='https://evaluator.example/cdp?access_token=***'

python run_webretriever.py \
  --input data/data/protocol3.json \
  --output outputs/smoke_official_cdp \
  --cdp-url "$WEBRETRIEVER_CDP_URL" \
  --task-index 0 \
  --max-concurrency 1 \
  --max-steps 100 \
  --model-timeout 180 \
  --task-timeout 600
```

当前工程会从 CDP URL 的 `access_token` 查询参数提取 `X-Access-Token`，再通过 Playwright `connect_over_cdp` 连接；不要自行解析、硬编码或修改这段连接逻辑。

### 5.2 8 路并发检查

比赛通知说明官方会提供 8 个沙箱 CDP URL，并据此运行 8 个 Worker。正式前至少做一次并发容量验证：

- 模型 API 能承受 8 路同时请求；
- 每个 CDP URL 只分配一个 Worker；
- 不共享会污染题目状态的登录态、页面或下载目录；
- 单次模型请求不超过 180 秒；
- 单题不超过 100 步；
- 正式 CDP 模式不使用 `--rerun-failed`。

如果本地只有 1–3 个浏览器，先用相同逻辑降低 `--max-concurrency` 做功能测试；不要因本地资源不足而修改正式的多进程分配规则。

### 5.3 `WR-047` 入口契约

模板入口必须能接受下列调用形态：

```bash
bash scripts/run.sh <task_file> <output_dir> <cdp_url_1> <cdp_url_2> ...
```

其中 CDP URL 数量可变，最多 8 个。入口从仓库根目录执行，不能假设当前目录是 `src/`。本地可用一个自建 CDP 浏览器做接口联调，但正式冒烟应由提交系统注入官方 URL。

## 6. 腾讯云浏览器沙箱：需要知道什么

### 6.1 产品模型

腾讯云 Agent Runtime 将沙箱区分为 Tool 和 Instance；`browser` 类型用于浏览器访问、网页自动化、页面交互和截图，适合本题的 Web 任务。沙箱实例是实际运行环境，具有独立生命周期。详见[腾讯云 Agent Runtime 工具类型说明](https://cloud.tencent.com/document/product/1814/132209)和[基本概念](https://cloud.tencent.com/document/product/1814/123814)。

腾讯云官方浏览器操作文档把两条链路分开：`live URL` 用来查看浏览器界面，`CDP URL` 用来由 Playwright 操作页面。官方示例用 `connect_over_cdp`，并在请求中带 `X-Access-Token`。[浏览器操作文档](https://cloud.tencent.com/document/product/1814/123852)

### 6.2 网络和令牌的含义

腾讯云 Agent Runtime 的网络模式包括 `PUBLIC`、`SANDBOX` 和 `VPC`：需要访问目标网站或第三方模型 API 时，概念上必须具备公网出站；`SANDBOX` 模式不提供 Internet/VPC 出站。[网络模式文档](https://cloud.tencent.com/document/product/1814/132216)

腾讯云也提供按实例获取访问 Token 的 API，Token 绑定具体实例并有过期时间。[获取沙箱实例访问 Token](https://cloud.tencent.com/document/product/1814/124818)

这些是“自建腾讯云沙箱”时的产品知识，不是参赛者在正式评测中的额外步骤。比赛官方 Guide 明确说正式评测会自动传入云端浏览器 CDP URL、无需自行搭建和额外认证；因此比赛中不要创建自己的腾讯云实例，也不要用自建实例替换评测方 URL。[WebRetriever 官方评测指南](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/)

如果只想在自己的腾讯云沙箱中做连接验证，官方示例的核心形态是：

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(
        cdp_url,
        headers={"X-Access-Token": access_token},
    )
    context = browser.contexts[0]
    page = context.pages[0]
    page.goto("https://example.com")
    print(page.title())
    browser.close()
```

不要把真实 `access_token` 写进代码、README、日志、截图或聊天记录。

### 6.3 如果要自建腾讯云沙箱做独立联调

以下流程只适用于队伍自己拥有腾讯云 Agent Sandbox 资源、想在正式提交前复现 CDP 行为的情况，不适用于比赛正式评测：

1. 在 Agent 沙箱控制台创建 API Key，并通过密钥管理或环境变量保存；
2. 创建 `browser` 类型的 Sandbox Tool，按需要选择 `PUBLIC` 网络模式；
3. 启动 Sandbox Instance，等待实例进入 `RUNNING`；
4. 按实例 ID 获取访问 Token；
5. 以实例的 9000 端口生成 Live URL 和 CDP URL，用 Playwright 连接；
6. 测试结束后停止实例，避免留下无用资源和费用。

腾讯云公开 API 文档当前使用 `StartSandboxInstance` 启动实例；如果在不同教程中看到其他创建实例名称，应以当前 API 文档和控制台为准。[API 概览](https://cloud.tencent.com/document/api/1814/124833)、[启动沙箱实例](https://cloud.tencent.com/document/api/1814/124816)、[获取实例 Token](https://cloud.tencent.com/document/api/1814/124818)。

自建沙箱能验证的能力主要是：Playwright 导航、点击、截图、request/response 监听以及本地轨迹落盘。腾讯云公开浏览器文档没有保证跨实例 Cookie/Profile 持久化，也没有把录屏作为浏览器沙箱的统一内置能力；因此本项目应以逐步截图和 `capture.json` 为可靠证据，不要把跨题登录态或远程录屏当成前置条件。[Playwright 网络监听](https://playwright.dev/python/docs/network)、[Playwright 截图](https://playwright.dev/python/docs/screenshots)、[腾讯云存储挂载](https://cloud.tencent.com/document/product/1814/132215)

### 6.4 本次比赛环境的已知信息

根据组委会在提交群中的答复：

- 云端浏览器不是无头运行；
- 默认浏览器窗口为 1920×1080；
- 没有确认具体显示后端是 Xvfb、X11 还是 Wayland，因此 Agent 不应依赖某一个显示服务器的内部行为；
- 风控/人工点击验证属于真实 Web 环境和考查点。

工程上应优先使用语义元素、DOM 文本和当前截图中的坐标，而不是写死另一种分辨率下的坐标。验证控件出现时，做一次有限的可见交互并等待结果；如果仍是 `Access Denied`、429/503 或反复无变化，应截图、记录 URL/状态并停止无效点击，给该题留下可审计失败证据。

## 7. 正式提交冒烟流程

### 步骤 A：推送最新代码

```bash
git add <实际修改文件>
git commit -m "prepare WebRetriever smoke test"
git push origin main
git log -1 --oneline origin/main
```

评测系统只读取 `main` 的最新 commit；本地未 push 的修改不会进入冒烟测试。推送后不要立刻在其他分支继续假设系统已经拿到新版本，先确认远端 commit 与本地一致。

### 步骤 B：在专属提交群触发

可先向 `@WR-EvalBot` 发送“你是谁”确认 Bot 身份和可用指令；正式触发时发送“提交代码”。按聊天记录，系统随后会执行：

```text
拉取代码 → 语法检查 → 环境安装 → 冒烟测试 → 返回通过/失败
```

一次提交只对应一次明确的代码快照。若失败，保留 Bot 的完整错误文本，修复后重新 push `main`，再按组委会允许的流程重新提交；不要在正式评测中把失败任务自动重跑当作补救方案。

### 步骤 C：根据结果定位

| 现象 | 首要检查 | 处理方式 |
| --- | --- | --- |
| 找不到文件/入口 | `config.json`、`environment.yml`、`scripts/run.sh`、`src/` | 从仓库根目录执行 `test`、`bash -n`、`compileall`，确认已 push 到 `main` |
| 依赖安装失败 | `environment.yml` 中 conda/pip 包、版本和系统依赖 | 锁定可安装版本，避免把本地未声明依赖当成已安装 |
| 模型连接失败 | API base、密钥、公网可达性、域名白名单、并发容量 | 从外部机器做接口健康检查；向表单补充实际入口域名 |
| CDP 连接失败 | 是否误改连接初始化、是否泄漏/破坏 URL、是否自行替换浏览器 | 恢复模板连接逻辑；正式环境等待评测方传入 URL，不自行创建实例 |
| 页面打不开或风控 | 起始 URL、页面截图、状态码、可见验证控件 | 采用正常 Playwright 交互和有限等待；不要使用搜索引擎、代理轮换或验证码代解 |
| `agent_answer` 为空 | 答案提取和 `result.json` 写入路径 | 在完成导航后显式提取并写入；不能为空 |
| 超过 100 步/单次模型超时 | Prompt 循环、重复滚动、重复点击、模型服务延迟 | 先做早停、证据检查和有限退避；不能靠重跑规避限制 |
| 只有部分题有产物 | Worker/CDP URL 数量、权限、任务目录并发写入 | 确认每个任务目录独立，检查 `logs/summary.json` 和 Worker 日志 |

## 8. 反爬和验证的合规处理策略

每道题建议遵循以下顺序：

1. 从题目给出的起始站点打开页面；
2. 先读当前页面 DOM、截图和站内导航控件；
3. 发现验证控件时，确认它是当前页面可见交互的一部分，进行一次准确点击或必要的等待；
4. 观察 URL、页面文本和网络响应是否发生有效变化，并保存截图/动作/请求记录；
5. 页面仍未放行时，有限重试后结束该题，不连续消耗 100 步。

明确禁止或不应作为默认提交方案：

- 访问外部通用搜索引擎获取答案；
- 用 `requests`、`curl` 或站外代理绕过浏览器流程直接抓目标内容；
- 轮换住宅代理或出口 IP；
- 注入/伪造浏览器指纹、使用 stealth/定制 Chromium；
- 把 CAPTCHA/人工验证交给第三方解题服务；
- 把另一台本地浏览器或自建腾讯云实例接入正式评测。

这些做法要么违背公开规则的“禁止搜索引擎”和 Playwright/CDP 约束，要么改变官方浏览器的可审计身份；若要评估任何特殊库或代理，应先取得组委会书面确认。

## 9. 最终验收清单

### 代码和配置

- [ ] `WR-047` 的 `main` 已推送最新 commit。
- [ ] `config.json`、`environment.yml`、`scripts/run.sh`、`src/` 都存在。
- [ ] `scripts/run.sh` 位置参数未变，支持可变数量 CDP URL。
- [ ] 模型 API 公网可达，能够承受最多 8 路请求。
- [ ] 算法服务入口域名已提交白名单表单。
- [ ] 没有提交 `.env`、API key、CDP token、私人联系方式或测试产物。

### 运行约束

- [ ] 使用 Playwright 操作官方 CDP 浏览器。
- [ ] `agent_answer` 在每题结果中非空。
- [ ] 单题最多 100 步，单次模型请求不超过 180 秒。
- [ ] 正式 CDP 运行最多 8 路并发，不启用失败题自动重跑。
- [ ] 只从指定站点及其可见的第一方页面/响应获取证据。
- [ ] 风控验证有截图、动作和结果等待记录。

### 输出结构

每题目录应类似：

```text
{output_dir}/{task_idx}_{task_id}/
├── result.json       # 唯一评分核心文件
├── trajectory/       # 每步原始截图
├── capture.json      # XHR/Fetch 捕获
└── trajectory_visual/ # 推荐：动作标注截图
```

官方 Guide 还要求结果目录、截图、动作记录和网络请求由 Agent 代码写入，评测系统自动读取；正式运行后应保留这些证据，不要为“重跑”删除已有产物。[官方输出格式说明](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/#faq)

## 参考资料

- 本地约束和组委会通知：[AGENTS.md](../../AGENTS.md)、[聊天记录.md](../../聊天记录.md)。
- 本地实现说明：[WEBRETRIEVER_CHALLENGE.md](../../WEBRETRIEVER_CHALLENGE.md)、[webretriever.toml](../../webretriever.toml)。
- 队伍提交模板：[hhhhhhalf/WR-047 README.md](https://github.com/hhhhhhalf/WR-047/blob/main/README.md)。
- 比赛官方规则：[WebRetriever Challenge 评测指南](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/)。
- 腾讯云：[Agent Runtime 工具类型](https://cloud.tencent.com/document/product/1814/132209)、[浏览器操作](https://cloud.tencent.com/document/product/1814/123852)、[网络模式](https://cloud.tencent.com/document/product/1814/132216)、[获取沙箱实例 Token](https://cloud.tencent.com/document/product/1814/124818)。
