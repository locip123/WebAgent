# WebRetriever Protocol III 比赛 Agent

`webretriever` 是本仓库面向 WebRetriever Challenge Protocol III 的独立浏览器 Agent。它只通过 Playwright 操作浏览器：从题目给出的起始网站出发，完成站内导航、信息提取和可追溯取证，并按比赛目录格式输出结果。

- 比赛主页：[WebRetriever Challenge](https://mininglamp-ai.github.io/WebRetriever_Challenge/)
- 官方规则与提交格式：[Challenge Guide](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/)
- 任务数据：`data/data/protocol3.json`
- 唯一运行入口：`python run_webretriever.py`

> 以官方 Guide 和正式评测提供的入口约定为最终准则。本文件说明当前仓库中的实现与本地开发流程。

## 1. 比赛约束与实现保证

| 比赛要求 | 当前实现 |
| --- | --- |
| 浏览器交互必须使用 Playwright + CDP | 独立的 Playwright 运行时；正式评测连接主办方提供的 CDP 浏览器 |
| 不得使用外部通用搜索引擎 | Prompt 与运行时均拦截常见外部搜索/答案引擎；起始站自身搜索可用 |
| 每题最多 100 步 | CLI 与 runner 均限制为 1–100 步 |
| 单次模型调用最多 180 秒 | 客户端与外层超时共同约束 |
| 最多 8 路并发 | worker、CDP URL 和 VLM 端口均限制为最多 8 个 |
| 正式任务失败不得自动重跑 | CDP 模式拒绝 `--rerun-failed`，每题只从队列领取一次 |
| 输出最终答案和轨迹 | `result.json`、`capture.json`、原始与可视化双轨截图均按任务保存 |
| `SUCCESS` 的判定 | 必须同时存在非空 `agent_answer` 和至少一条浏览器来源证据 |
| 公开答案不得泄漏给 Agent | loader 仅保留任务元数据；`answer` 不会进入 prompt 或任务序列化 |

真实网站可能出现反爬、风控或需要人工点击验证。这是比赛 Web 环境的一部分，Agent 应在合规的浏览器交互范围内识别和处理；不能依赖外部搜索引擎绕过任务。

## 2. 环境安装

```bash
conda activate Browser-Use
cd /workspace/code/browser-use
```

当前环境使用 Python 3.12。新机器可按以下方式安装：

```bash
conda create -n Browser-Use python=3.12 -y
conda activate Browser-Use
uv pip install --python "$CONDA_PREFIX/bin/python" -e '.[core]' litellm 'httpx[socks]'
uvx playwright install chromium --with-deps
```

正式评测连接主办方提供的云端浏览器，通常不需要本机安装 Chromium；仅本地浏览器调试时才需安装：

```bash
python -m playwright install chromium
```

## 3. 模型与敏感配置

程序会自动加载根目录的 `.env`。推荐配置 OpenAI-compatible Responses API：

```dotenv
WEBRETRIEVER_API_KEY=your-api-key
WEBRETRIEVER_API_BASE=http://127.0.0.1:8317/v1
WEBRETRIEVER_MODEL=gpt-5.6-luna
WEBRETRIEVER_API_MODE=responses
WEBRETRIEVER_REASONING_EFFORT=low
```

也兼容 LiteLLM / OpenAI 常见变量：`LITELLM_MODEL`、`LITELLM_BASE_URL`、`LITELLM_MASTER_KEY`、`OPENAI_MODEL`、`OPENAI_BASE_URL` 与 `OPENAI_API_KEY`。命令行的 `--api-key`、`--api-base`、`--model`、`--api-mode`、`--reasoning-effort` 优先级更高。

当前本机代理可用 `gpt-5.6-luna`（默认）或 `gpt-5.4`，例如：

```bash
python run_webretriever.py --config webretriever.toml --model gpt-5.4
```

模型版本是否合规则以 Guide 为准。请勿提交 `.env`，也不要在日志、终端截图或对话中暴露 API key、CDP URL 中的 `access_token` 或真实联系方式。

### SEC EDGAR 任务

访问 SEC 域名的任务应提供真实的组织名和联系邮箱：

```bash
export WEBRETRIEVER_SEC_USER_AGENT='Your Organization sec-admin@your-domain.example'
```

该请求头仅用于 SEC 及其子域，并会在产物记录中脱敏。未设置时程序会警告但继续运行；SEC 可能拒绝匿名自动化流量。多个 SEC 任务会自动串行，以降低共享 IP 的速率限制风险。

## 4. 浏览器与 CDP

CDP（Chrome DevTools Protocol）是 Chrome 的远程控制通道；Playwright 通过它完成点击、输入、截图和页面读取。正式评测时主办方会将 CDP URL 传给运行脚本，本地开发可自行启动 Chrome。

Linux 示例：

```bash
google-chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="/workspace/code/browser-use/tmp/chrome-debug-profile" \
  --no-first-run \
  --no-sandbox
```

验证本地 CDP 是否可用：

```bash
curl http://127.0.0.1:9222/json/version
```

返回含 `webSocketDebuggerUrl` 的 JSON 即表示通道就绪。项目也提供了用于本机三浏览器开发环境的服务脚本：

```bash
./start_webretriever_services.sh
```

该脚本会管理本机代理、辅助服务及端口 `9222`、`9223`、`9224` 的 Chrome 实例；保持前台运行，按 `Ctrl+C` 停止它启动的服务。

### 4.1 虚拟显示中的有头 Chrome：风控验证对照测试

```bash
conda activate Browser-Use
cd /workspace/code/browser-use

PROFILE_DIR="$(mktemp -d "$PWD/tmp/webretriever-headed-profile.XXXXXX")"
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp >tmp/xvfb-webretriever.log 2>&1 &
export DISPLAY=:99

google-chrome \
  --no-sandbox \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9233 \
  --user-data-dir="$PROFILE_DIR" \
  --no-first-run \
  --no-default-browser-check \
  about:blank >tmp/chrome-headed-9233.log 2>&1 &
curl -fsS http://127.0.0.1:9233/json/version \
  | jq '{Browser, user_agent: .["User-Agent"]}'
```

```bash
python run_webretriever.py \
  --config webretriever.toml \
  --input data/data/protocol3.json \
  --output outputs/rebrowser_1x1_headed_retry \
  --cdp-url http://127.0.0.1:9233 \
  --task-index 55,57,69,76,84,96 \
  --max-concurrency 1 \
  --rebrowser-experiment
```

```bash
jq . outputs/rebrowser_1x1_headed_retry/experiment_summary.json
```

## 5. 集中运行配置

根目录的 [`webretriever.toml`](webretriever.toml) 集中管理任务文件、产物目录、模型接口、浏览器、并发、步骤数、超时、筛题和 Prompt 日志。默认配置面向安全的本地前三题开发：

```bash
python run_webretriever.py --config webretriever.toml
```

命令行参数可以临时覆盖配置，例如：

```bash
python run_webretriever.py --config webretriever.toml --task-index 9
```

布尔配置也可反向覆盖，如 `--no-local-browser`、`--no-headed`、`--no-structured-prompt-log`。配置文件会严格校验未知字段和错误类型。

正式评测时，设置 `local_browser = false`，提供 `cdp_urls`（或不配置它以读取 `WEBRETRIEVER_CDP_URLS`、`WEBRETRIEVER_CDP_URL`、`CDP_URL`），并将 `task_indices` 设为 `[]` 以运行全部任务。真实 API key 与 SEC 联系方式应只保留在 `.env` 或环境变量中。

## 6. 运行流程

### 6.1 先校验任务文件

此操作不启动浏览器、不调用模型，也不会将公开答案发给 Agent：

```bash
python run_webretriever.py \
  --input data/data/protocol3.json \
  --output outputs/validate \
  --validate-only
```

当前 `protocol3.json` 应加载 100 个任务，并显示：

```json
"ground_truth_exposed_to_agent": false
```

### 6.2 本地冒烟测试

通过 Playwright 启动本地 Chromium，测试前三题：

```bash
python run_webretriever.py \
  --input data/data/protocol3.json \
  --output outputs/protocol3_first3_local \
  --local-browser \
  --task-index 0-2 \
  --api-mode responses \
  --reasoning-effort low \
  --max-steps 100 \
  --model-timeout 180 \
  --task-timeout 300
```

`--task-index` 支持单值、逗号和闭区间，例如 `3` 或 `1,4-6`。开发时可加 `--headed` 观察页面，并且只有 `--local-browser` 模式允许 `--rerun-failed` 覆盖失败任务。

`SUCCESS` 仅代表 Agent 得到了非空答案和浏览器证据，并不代表已与公开参考答案完成离线比对。

### 6.3 正式 CDP 运行

评测方提供 URL 后，在当前终端配置：

```bash
export CDP_URL='evaluator-provided-cdp-url'
```

运行全部任务：

```bash
python run_webretriever.py \
  --input data/data/protocol3.json \
  --output outputs/protocol3 \
  --cdp_url "$CDP_URL" \
  --api-mode responses \
  --max-steps 100 \
  --model-timeout 180
```

调试指定题目时才加筛选，例如 `--task-index 4-6`。多个 CDP 地址可提升并行度，实际 worker 数受 CDP URL 数量和 `--max-concurrency`（1–8）共同限制：

```bash
python run_webretriever.py \
  --input data/data/protocol3.json \
  --output outputs/protocol3 \
  --cdp_url http://127.0.0.1:9222 http://127.0.0.1:9223 http://127.0.0.1:9224 \
  --max-concurrency 3 \
  --api-mode responses
```

也可用空格或逗号分隔的 `WEBRETRIEVER_CDP_URLS` 提供多个地址。正式 CDP 模式禁止 `--rerun-failed`；已有终态 `result.json` 的任务会跳过，保留为 `PENDING` 的任务可用相同命令继续执行。

### 6.4 自部署 OpenAI-compatible VLM

可配置最多 8 个本地端口，按 worker 轮询；使用端口时不能同时指定 `api_base`：

```bash
python run_webretriever.py \
  --input data/data/protocol3.json \
  --output outputs/protocol3_vllm \
  --local-browser \
  --vlm_ports 8000 8001 \
  --model your-model \
  --api-mode chat-completions \
  --task-timeout 1200
```

自部署模型的权重提交与复现责任以 Guide 为准；runner 只处理接口连接与运行限制。

## 7. 输出、图表数据与恢复

每个任务使用 `{task_idx}_{task_id}` 目录：

```text
OUTPUT_DIR/
├── 0_<task_id>/
│   ├── result.json              # 状态、动作、答案、证据、耗时、token 使用量
│   ├── capture.json             # Playwright 捕获的 XHR/Fetch 请求与有界响应体
│   ├── model_prompts.json       # 可复现的模型输入轨迹
│   ├── trajectory/              # 未标注的原始逐步截图
│   ├── trajectory_visual/       # 带元素编号和动作标签的截图
│   ├── downloads/               # 浏览器下载的文档
│   └── chart_data/<scan_id>/    # 图表网络包、规范化数据和分析产物
├── locks/                       # 每题 advisory lock
└── logs/
    ├── summary.json             # 本次选中任务的状态汇总
    └── worker_<id>_<date>.log
```

`result.json` 的 `duration_seconds` 记录 Agent 循环耗时；`task_started_at`、`task_completed_at`、`task_elapsed_seconds` 记录端到端耗时。超出 `task_timeout_seconds` 会写入 `FAIL_TASK_TIMEOUT`。常见状态还有 `PENDING`、`SUCCESS`、`FAIL`、`FAIL_MODEL`、`FAIL_MODEL_TIMEOUT`、`FAIL_BROWSER`、`FAIL_MAX_STEPS` 和 `FAIL_RUNTIME`。

下载内容支持提取 PDF、TXT、CSV、JSON、DOCX、XLS/XLSX 和 ZIP 内的文本/CSV。图表任务会先用 `find_chart_data_requests` 保存数据请求，再以该任务返回的绝对 `data_dir` 调用 `call_data_analysis_assistant`。分析助手只读取 manifest 中列出的 CSV，并校验任务身份、路径、符号链接和 SHA-256；受限 SQL 在禁用外部访问的隔离 DuckDB 子进程中执行。无法可靠还原表格时会标记为 `saved_raw_only`，再回退到 cursor、DOM 表格或 tooltip。

默认 `model_prompts.json` 是 `webretriever-model-prompts/v2-lines`：系统提示和每步 prompt 按真实换行写为字符串数组，可用 `"\\n".join(prompt)` 精确还原。添加 `--structured-prompt-log` 后切换为 `v2-structured`，额外记录字符/token 统计、启用 playbook、裁剪原因及每次模型调用的耗时、usage 与错误。

前台运行时按 `Ctrl+C` 可中止。已完成任务产物会保留，当前任务通常保持为 `PENDING`；使用相同输入、输出目录和命令可恢复。正式比赛不要删除已有产物后重跑。

快速检查结果：

```bash
jq . outputs/protocol3/logs/summary.json
find outputs/protocol3 -path '*/result.json' -exec jq -r '[.task_idx,.status,.agent_answer] | @tsv' {} \;
```

## 8. 测试与代码检查

```bash
python -m pytest -q \
  tests/ci/test_webretriever_models.py \
  tests/ci/test_webretriever_browser.py \
  tests/ci/test_webretriever_agent.py \
  tests/ci/test_webretriever_network.py \
  tests/ci/test_webretriever_chart_data.py \
  tests/ci/test_webretriever_data_analysis.py \
  tests/ci/test_webretriever_runner.py \
  tests/ci/models/test_openai_responses_api.py

python -m ruff check browser_use/webretriever browser_use/llm/openai/chat.py
python -m pyright browser_use/webretriever browser_use/llm/openai/chat.py
```

这些测试不需要真实模型，也不会访问比赛站点。若只验证任务文件是否可安全加载，使用第 6.1 节的 `--validate-only`。

## 9. 提交前检查

- 对照最新官方 Guide，确认模型版本、自部署模型要求、入口名和提交格式。
- 正式运行使用评测方 CDP 地址，保留 `--max-steps 100`，并确保没有 `--rerun-failed`。
- 不使用外部通用搜索引擎；所有浏览器证据均来自任务允许的站内浏览。
- 检查 `logs/summary.json` 和每题 `result.json`，确认成功任务同时包含答案与证据。
- 不提交 `.env`、真实 API key、带令牌的 CDP URL 或个人联系信息。
