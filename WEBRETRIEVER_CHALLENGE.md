# WebRetriever Protocol III 比赛 Agent

本仓库新增了独立的 `webretriever-agent` 入口。它不会调用 Browser-Use 原有的默认搜索工具，而是用一个只通过 Playwright 操作浏览器的比赛运行时，完成“导航到正确页面 + 提取最终答案”两部分任务。

比赛主页：[WebRetriever Challenge](https://mininglamp-ai.github.io/WebRetriever_Challenge/)；规则与提交格式以[官方 Guide](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/)为准。官方模板尚未发布时，本入口同时兼容开源参考实现的 `--input --output --cdp_url` 参数。

## 已落实的比赛约束

| 规则 | 实现 |
|---|---|
| 浏览器交互只能使用 Playwright | `browser_use/webretriever/browser.py` 是独立 Playwright runtime；不复用项目原有的 CDP 操作链 |
| 禁止外部搜索引擎 | prompt 与运行时双重拦截常见搜索/答案引擎；目标站自己的搜索允许使用 |
| 每题最多 100 步 | CLI 和 runner 均硬限制为 `1..100` |
| 单次模型请求最多 180 秒 | OpenAI-compatible client 与外层 `asyncio.wait_for` 双重限制 |
| 最多 8 路并发 | CDP URL、worker 和 CLI 配置均限制为最多 8 |
| 不得重试任务 | 正式 CDP 模式拒绝 `--rerun-failed`；一个任务只从队列领取一次 |
| Protocol III 最终答案 | `result.json` 写入 `agent_answer`，并要求非空浏览器证据后才可 `SUCCESS` |
| 原始/可视化轨迹 | 每步分别写入 `trajectory/` 与 `trajectory_visual/` |
| XHR/Fetch 记录 | 请求、响应状态及有界响应正文写入 `capture.json` |
| 标准答案隔离 | 输入 loader 只保留 `task_idx/task_id/website/task`；公开数据里的 `answer` 永远不会进入模型 prompt 或任务序列化 |
| 崩溃与断点安全 | JSON 原子落盘、每题 advisory lock、成功/失败结果默认均不自动重跑 |

模型提示还覆盖多条件筛选复核、分页与 top-N、跨页聚合、PDF/Excel 表头及单位、图表 tooltip、XHR 参数对应关系和网页 prompt injection。

比赛任务基于真实网站构建，网站出现的反爬/风控机制（需要人工点击验证）属于真实 Web 环境的一部分，没法完全避免，需要用户设计agent去解决这个验证。处理反爬/风控机制是本次挑战赛的核心考点之一。

📖 备赛导读 #3｜模型和环境——哪些你自备、哪些官方给

一、一句话总结

· 你出：模型推理资源（API 或自部署）
· 官方出：云端浏览器沙箱 + 评测任务 + 评分系统

你的 Agent 通过 OpenAI 兼容 API 调用模型，浏览器操作走 CDP。两条线互不干扰。

二、模型资源（你自己管）

Agent 必须通过 OpenAI 兼容格式的 API 调用模型。两种选择：

选项 A：商业闭源 API
各厂商允许使用的最高版本：
· OpenAI — GPT-5.4
· Anthropic — Claude 4.6
· Google — Gemini 3.1
· xAI — Grok 4.3
· 智谱 AI — GLM-5V-Turbo
· Moonshot — Kimi-K2.6
· 阿里云 — Qwen3.7（新增）
⚠️ 超出上述版本禁止使用。未列出的厂商不限版本。

选项 B：自研/自部署模型
允许，赛后需提交模型权重供组委会校验。

⚠️ 组委会会审查模型调用情况，严查套壳和中转绕版本限制，违规成绩无效。

三、浏览器环境（备赛 vs 正式）

· 备赛（现在）：你自己起 Chrome + CDP（上期已教）
· 正式评测：官方提供云端沙箱，CDP URL 通过命令行参数自动传入你的脚本

你的代码只需要能接收 CDP URL 就行，备赛和正式的逻辑不用改。

四、run_agent.sh 怎么配

这是你 Agent 的入口脚本。正式评测时，系统通过命令行参数传入三样东西：
· 任务文件路径
· 输出目录路径
· CDP URL（云端浏览器地址）

备赛时你手动填这些值，比如：
CDP_URLS=("http://localhost:9222") ← 本地 Chrome 地址
模型配置填你自己的 API endpoint 和 key。

正式评测时这些由系统传入，你不需要硬编码。

五、关键限制

· 最多 8 个任务并发
· 单次模型请求超时 3 分钟
· 每个任务最多 100 步
· 失败不重试，该任务计 0 分，但不影响其他任务得分
· 禁止使用搜索引擎（赛后验证操作轨迹）

六、第三方框架可以用吗？

可以。Browser Use、Playwright 原生、或任何你自己的框架都行。只要满足：
· 接受 CDP URL 作为输入
· 按规定格式输出结果到指定目录
框架不限，模型不限（在版本规则内）。

🚀 现在可以做：
① 确定你要用什么模型（商业 API 还是自部署），拿到 API key / endpoint
② 在 run_agent.sh 里配好模型地址 + 本地 CDP URL
③ 跑一次开源项目示例，确认模型调用 + 浏览器控制都通

 备赛导读 #2｜Playwright 与 CDP——动手前必须搞定的前提环境

一、为什么这是硬前提

评测指南原文：
⚠️ 评测环境通过 CDP 提供云端浏览器，Agent 必须基于 Playwright 进行所有浏览器交互操作。

不管你用什么模型、什么框架，操作浏览器只能走 Playwright + CDP 这条路。正式评测时官方给你一个云端浏览器的 CDP 地址，你的代码连上去就能操作。

备赛第一步不是调模型，是先把 Playwright 连 CDP 跑通。这个没通，后面一切跑不起来。

二、两个概念

· CDP（Chrome DevTools Protocol）— Chrome 的远程控制接口。给 Chrome 加一个启动参数，它就在某个端口监听，允许外部程序控制（点击、输入、截图等）。
· Playwright — 浏览器自动化库（Python），通过 CDP 连接 Chrome 并用代码控制。开源项目 src/agent/web_controller.py 就是基于它实现的。

关系：CDP 是通道，Playwright 是工具。

三、本地怎么搞（备赛调试用）

起一个带调试端口的 Chrome：

macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="/tmp/chrome-debug-profile"

Linux
google-chrome --remote-debugging-port=9222 --user-data-dir="/tmp/chrome-debug-profile" --no-first-run --no-sandbox

Windows（CMD）
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\temp\chrome-debug-profile"
（Chrome 已加入 PATH 的话，直接 chrome --remote-debugging-port=9222 --user-data-dir="C:\temp\chrome-debug-profile"）

✅ 验证（三个系统通用）
curl http://localhost:9222/json/version
返回一段 JSON（含 webSocketDebuggerUrl 字段）= CDP 通道就绪

⚙️ 配置
在 scripts/run_agent.sh 里填：
CDP_URLS=("http://localhost:9222")

四、备赛 vs 正式评测

· 备赛：你自己本地起 Chrome + CDP，自己配地址
· 正式：官方提供云端沙箱，CDP 地址自动传入，代码逻辑不用改

🚀 现在可以做：
① 按你的系统（macOS/Linux/Windows）起带 --remote-debugging-port=9222 的 Chrome
② curl http://localhost:9222/json/version 验证连通
③ 没装 Playwright：pip install playwright && playwright install chromium

搞定后就具备跑通开源项目示例的前提条件。卡在哪一步群里问。

📢 后续还有更多备赛导读，持续更新中。

## 环境安装

```bash
conda activate Browser-Use
cd /workspace/code/browser-use
uv pip install --python "$CONDA_PREFIX/bin/python" -e .
```

正式评测连接主办方提供的 CDP 浏览器，不需要在本机安装 Chromium。仅在本地调试时执行：

```bash
python -m playwright install chromium
```

## 模型配置

支持 OpenAI-compatible Responses API 或 Chat Completions。当前项目的 LiteLLM Responses API 可使用：

```dotenv
LITELLM_MODEL=gpt-5.4
LITELLM_BASE_URL=http://127.0.0.1:4000/v1
LITELLM_MASTER_KEY=your-key
WEBRETRIEVER_API_MODE=responses
```

也可以使用 `WEBRETRIEVER_MODEL`、`WEBRETRIEVER_API_BASE`、`WEBRETRIEVER_API_KEY`。环境变量比把密钥写进命令历史更安全；runner 不会在日志中输出 API key，并会遮盖 CDP URL 中的 `access_token`。

模型的 `thought` 字段默认使用中文（最终 `answer` 仍按任务语言输出）。如需覆盖，可传 `--thought-language English`，或设置 `WEBRETRIEVER_THOUGHT_LANGUAGE=English`；命令行优先。每个 `result.json` 也会记录实际使用的 `thought_language`，便于复现。

请遵守 Guide 公布的闭源模型版本上限。runner 会拒绝能够明确识别为高于上限的模型名，但最终合规责任仍在参赛队伍。

## 先验证任务文件

这一步不启动浏览器、不调用模型，也不会显示公开数据中的标准答案：

```bash
python run_webretriever.py \
  --input data/data/protocol3.json \
  --output outputs/protocol3 \
  --validate-only
```

预期显示 100 个合法任务及 `ground_truth_exposed_to_agent: false`。

## 本地单题冒烟测试

```bash
python run_webretriever.py \
  --input data/data/protocol3.json \
  --output outputs/local-smoke \
  --local-browser \
  --task-index 0 \
  --max-steps 20 \
  --api-mode responses
```

`--task-index 3,6-8` 支持逗号和闭区间。`--limit`、`--headed`、`--rerun-failed` 都只用于本地开发；其中失败任务重跑在正式比赛中不允许。

## 正式 CDP 运行

单浏览器：

```bash
python run_webretriever.py \
  --input "$TASK_FILE" \
  --output "$OUTPUT_DIR" \
  --cdp_url "$CDP_URL" \
  --model "$WEBRETRIEVER_MODEL" \
  --api-mode responses
```

最多 8 个浏览器并发：

```bash
python run_webretriever.py \
  --input "$TASK_FILE" \
  --output "$OUTPUT_DIR" \
  --cdp_url "$CDP_URL_1" "$CDP_URL_2" "$CDP_URL_3"
```

自部署的 OpenAI-compatible vLLM 也兼容参考实现的端口参数；多个端口按 worker 轮询分配，默认使用 Chat Completions：

```bash
python run_webretriever.py \
  --input "$TASK_FILE" \
  --output "$OUTPUT_DIR" \
  --cdp_url "$CDP_URL_1" "$CDP_URL_2" \
  --model your-served-model-name \
  --vlm_ports 8000 8001
```

按 Guide 要求，自部署模型需要提交可复现的模型权重；runner 只负责连接与限额校验。

也可以使用安装后的命令：

```bash
webretriever-agent --input "$TASK_FILE" --output "$OUTPUT_DIR" --cdp_url "$CDP_URL"
```

不要在终端输出、提交记录或问题截图中暴露带 `access_token` 的 CDP URL。主办方发布最终模板后，应再次对照其入口文件名和新增参数；当前入口已经保留参考实现的标准参数名。

## 输出目录

```text
OUTPUT_DIR/
├── 0_<task_id>/
│   ├── trajectory/          # 原始逐步截图
│   ├── trajectory_visual/   # 带元素编号和动作标注的截图
│   ├── downloads/           # 浏览器轨迹中下载的文档
│   ├── result.json          # 状态、动作、URL、agent_answer、evidence
│   ├── capture.json         # XHR/Fetch 轨迹
│   └── model_prompts.json   # 可复现的结构化模型输入调试轨迹
├── locks/                   # 每题 advisory lock 标记
└── logs/
    ├── worker_*.log
    └── summary.json
```

`SUCCESS` 必须同时有最终答案和至少一条带来源上下文的证据。模型超时、浏览器异常、连续动作失败或耗尽步数都会保留可诊断的失败状态和已有轨迹。

默认的 `model_prompts.json` 使用 `webretriever-model-prompts/v2-lines`：`system_prompt` 与每步 `prompt` 都是逐行字符串数组，因此原始 prompt 中的每一个换行都会在 JSON 中显示为一行，避免把整段内容压成带大量 `\n` 的单一字符串。用 `"\n".join(prompt)` 可精确还原实际发送的文本。若需要详细调试 schema（任务、执行状态、浏览器观测、信任级别、字符/token 统计、启用的 playbook、裁剪原因，以及模型调用耗时/usage/error），显式添加 `--structured-prompt-log`；它会输出 `webretriever-model-prompts/v2-structured`。

## 测试

```bash
python -m pytest -q \
  tests/ci/test_webretriever_models.py \
  tests/ci/test_webretriever_browser.py \
  tests/ci/test_webretriever_agent.py \
  tests/ci/test_webretriever_runner.py

python -m ruff check browser_use/webretriever tests/ci/test_webretriever_*.py run_webretriever.py
python -m pyright browser_use/webretriever run_webretriever.py
```

测试不需要真实模型，也不会访问比赛站点。若只想验证公开 Protocol III 数据是否能安全加载，运行上面的 `--validate-only` 即可。


