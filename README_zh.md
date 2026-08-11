# WebRetriever Challenge Template

🔤 中文 | [English](README.md)

[WebRetriever Challenge](https://mininglamp-ai.github.io/WebRetriever_Challenge/) 比赛提交模板，基于 [WebRetriever](https://github.com/Mininglamp-AI/WebRetriever) 框架，包含完整的 [UI-TARS 1.5](https://github.com/bytedance/UI-TARS) 参考 Agent 实现。

## ⚡ 你需要做什么

```
1. 将本仓库 clone 到本地 → git clone <你的队伍 repo 地址>
2. 编辑 config.json → 填入你的模型 API
3. 编辑 environment.yml → 声明运行环境依赖（conda + pip）
4. 自定义 Agent (src/agent/) → 实现你的逻辑 + 答案提取
5. Push 到 `main` 分支（评测系统只读取 `main` 分支，不要创建其他分支）
```

> 💡 比赛采用 **Protocol III（端到端任务协议）**：仅导航到目标页面**并不足够**，还需从页面中提取答案并写入 `result.json` 的 `agent_answer` 字段。模板未实现该步骤，**需由你自行完成**。

## ⚠️ 注意事项

| 事项 | 说明 |
|------|------|
| **必须配置 `config.json`** | 填入你的模型 API（见下方「配置说明」），未配置则 Agent 无法启动 |
| **必须配置 `environment.yml`** | 声明运行环境（conda + pip），评测系统据此安装依赖 |
| **`run.sh` 接口不可更改** | 默认脚本可直接使用，允许添加准备步骤，但位置参数接口不可改动（详见下方「评测接口」） |
| **必须填写 `agent_answer`** | 评分仅看两点：① 是否**成功导航到目标页面** ② `agent_answer` 与标准答案**语义是否一致**。该字段留空计 0 分 |
| **每题 ≤ 100 步** | 超过 100 步该题**直接判 0 分**，不进入语义评分 |
| **请勿改动沙箱连接逻辑** | `web_controller.py` 的 `init_playwright_context` 已适配远程沙箱，请勿修改其连接逻辑（详见下方「评测接口」） |

## 📁 目录结构

```
├── config.json                # 【必填】模型 API 配置
├── environment.yml            # 【必填】环境依赖（conda + pip 一份声明）
├── scripts/
│   └── run.sh                 # 【必填】评测入口脚本
├── src/
│   └── agent/
│       ├── main.py            # 多进程任务运行器（一般不需要改）
│       ├── agent.py           # Agent 核心逻辑（★ 重点自定义）
│       ├── prompts.py         # Prompt 模板（★ 重点自定义）
│       └── web_controller.py  # 浏览器控制（★ 重点自定义）
├── data/
│   └── example_tasks.json     # 示例任务（本地调试用）
└── LICENSE
```

## 📝 自定义 Agent

`src/agent/` 提供了基于 UI-TARS 1.5 的参考实现，你可以自由修改或替换：

| 文件 | 功能 | 可以改什么 |
|------|------|-----------|
| `agent.py` | 模型交互、动作解析、截图处理 | 换成你自己的模型/逻辑 |
| `prompts.py` | System/User prompt 模板 | 重新设计 prompt 策略 |
| `web_controller.py` | 浏览器操作（点击、输入、滚动、截图） | 扩展浏览器能力；⚠️ 请保留 `init_playwright_context` 的沙箱连接逻辑 |
| `main.py` | 多进程调度、结果保存 | 一般不需要改 |

**💡 使用其它框架？** 完全可以，把框架代码放到 `src/agent/` 下即可。无论用什么框架，以下三条硬性约束必须满足：

- **入口固定**：以 `scripts/run.sh` 作为评测入口，并兼容约定的位置参数（见下方「评测接口」）。
- **浏览器走 Playwright**：远程沙箱浏览器需通过 Playwright（CDP）连接与操作，连接请沿用 `init_playwright_context`（请勿改动其连接逻辑）。
- **产出路径固定**：每题在 `{output_dir}/{task_idx}_{task_id}/` 下写出 `result.json`（结果）、`trajectory/`（轨迹截图）、`capture.json`（捕获的网络请求）；另建议产出 `trajectory_visual/`（可视化截图，可选）。详见下方「输出格式」。

### `agent_answer` — 评分核心字段

Agent 导航到目标页面后，必须提取答案并写入 `agent_answer` 字段。提取方式不限：

- 截图 OCR
- DOM 文本解析
- VLM 视觉问答
- 网络请求捕获
- 或任意组合

模板中 `agent_answer` 默认留空，**需由你填写**。

## ⚙️ 配置说明（`config.json`）

> 💡 **注意并发**：评测时系统为每位参赛者提供最多 **8 个** CDP URL（即最多 8 路任务并行、同时请求模型），请确保你在此配置的模型能支撑这样的并发；自部署模型可通过 `api_ports` 增加实例分摊。

### 方式一：自部署模型（vLLM / SGLang）

```json
{
    "api_base": "http://<your-public-ip>",
    "api_key": "sk-abc123",
    "api_model": "uitars",
    "api_ports": [8001, 8002, 8003, 8004],
    "temperature": 0.7,
    "top_p": 1.0,
    "max_tokens": 8192
}
```

- `api_base`：仅填公网 IP（如 `http://203.0.113.1`）。
- `api_ports`：每张 GPU 一个端口，请求轮询分发。

> ⚠️ **必须是公网 IP。** 评测环境在云端沙箱中运行。部署后在另一台机器验证：`curl http://你的IP:8001/v1/models`

### 方式二：闭源 API（OpenAI / Claude / 通义）

```json
{
    "api_base": "https://api.openai.com/v1",
    "api_key": "sk-xxx",
    "api_model": "gpt-4o"
}
```

- `api_base`：完整地址**带 `/v1`**，直接传给 OpenAI SDK。不需要 `api_ports`。

## 📦 环境依赖（`environment.yml`）

评测系统据此文件自动创建专属 conda 环境（`conda env create -f environment.yml`），一份文件同时声明 conda 与 pip 依赖：

```yaml
dependencies:
  - python=3.10          # Python 版本
  # - nodejs             # 如需系统 / 其它语言工具，在此声明（走 conda-forge）
  - pip
  - pip:                 # Python 包在此声明；不指定版本即安装最新，如需锁定写作 numpy==1.26.4
      - playwright
      - openai
      - ...
```

- **不指定版本即安装最新**；如需锁定版本，写作 `包名==版本号`。
- 详细说明见文件内注释；如需新增依赖，在 `pip:` 下补充即可。

## 🔌 评测接口（务必兼容此调用格式）

评测系统会在**你的仓库根目录**下、用你 `environment.yml` 安装好的 conda 环境，按如下格式调用你的 `run.sh`：

```bash
bash scripts/run.sh <task_file> <output_dir> <cdp_url_1> <cdp_url_2> ...
```

| 参数 | 说明 |
|------|------|
| `task_file` | 本次要跑的任务 JSON 文件路径 |
| `output_dir` | 结果输出目录（每题写到 `output_dir/{task_idx}_{task_id}/result.json`）|
| `cdp_url_1 ...` | 浏览器 CDP URL 列表，**数量不固定、最多 8 个**（= 本次可用的并发 worker 数）|

**你的 `run.sh` 必须支持这套位置参数**（第 1 个 = 任务文件，第 2 个 = 输出目录，第 3 个起 = 不定长的 CDP URL 列表）。默认 `run.sh` 已将其透传给 `main.py`，可添加准备步骤，但**请勿更改该接口**。

> 🌐 **评测浏览器为远程腾讯云沙箱（非本地），通过 Playwright 连接。** 连接与鉴权已封装在 `web_controller.py` 的 `init_playwright_context` 中，模板 `main.py` 启动时会自动调用它——**默认情况下你无需自行处理连接**，直接编写点击 / 输入 / 滚动 / 解析等操作即可；但**请勿改动 `init_playwright_context` 的连接逻辑**，否则评测时无法连接沙箱、所有任务失败。

如确需自行再发起 `connect_over_cdp` 连接，务必带上 `X-Access-Token` header——复用 `get_cdp_headers()` 即可（`init_playwright_context` 首次连接时已自动缓存 token，无需自己解析）：

```python
from playwright.sync_api import sync_playwright
from web_controller import get_cdp_headers

p = sync_playwright().start()
# connect_over_cdp 必须携带鉴权 header，否则无法连接沙箱
browser = p.chromium.connect_over_cdp(cdp_url, headers=get_cdp_headers())
```

## 📥 输入格式（`tasks.json`）

```json
[
  {
    "task_idx": 0,
    "task_id": "cdfae1f0...",
    "website": "https://example.com",
    "task": "查询某某信息..."
  }
]
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `task_idx` | int | 任务序号（从 0 开始） |
| `task_id` | string | 任务唯一标识 |
| `website` | string | 起始 URL |
| `task` | string | 自然语言任务指令 |

> 部分任务可能包含 `key_points`（评分点提示），仅供参考。

## 📤 输出格式（`result.json`）

每道题在 `{output_dir}/{task_idx}_{task_id}/` 目录下产出，**这套保存路径务必保持一致**：

- `result.json` —— 该题结果（字段见下），**评分只读这个文件**
- `trajectory/` —— 每步执行的轨迹截图（`0.png`、`1.png` …）
- `capture.json` —— 捕获的页面网络请求
- `trajectory_visual/` —— 带动作标注的可视化截图（**可选，建议实现**，便于调试与复盘）

`result.json` 内容示例：

```json
{
    "task_idx": 0,
    "task_id": "cdfae1f0...",
    "task": "查询中国市场2025年中秋节当天...",
    "website": "https://example.com",
    "status": "SUCCESS",
    "reference_length": 100,
    "predict_length": 12,
    "agent_answer": "《731》",
    "final_result_response": "已定位到目标信息...",
    "actions": ["click(100, 200)", "type('查询')"],
    "thoughts": ["分析页面...", "找到目标..."],
    "history_resps": ["...", "..."],
    "urls": ["https://...", "https://..."]
}
```

**评分规则：**
- 查看是否**成功导航到目标页面**，且 `agent_answer` 字段的值与标准答案的**语义是否一致**。
- ⏱️ **每题最多 100 步**：超过 100 步该题**直接判 0 分，不进入语义评分**。

<details>
<summary>完整字段说明（点击展开）</summary>

| 字段 | 类型 | 说明 |
|------|------|------|
| `task_idx` | int | 任务序号 |
| `task_id` | string | 任务唯一标识 |
| `task` | string | 任务指令 |
| `website` | string | 起始网站 |
| `status` | string | `SUCCESS` / `FAIL` / `FAIL_SCROLLDOWN` / `FAIL_SAVE_SCREENSHOT_ERROR` |
| `reference_length` | int | 参考步数 |
| `predict_length` | int | 实际执行步数 |
| `agent_answer` | string | **【必填】** 提取的答案 — 评分核心字段 |
| `final_result_response` | string | Agent 最后一步的推理内容 |
| `actions` | list[str] | 每步执行的动作 |
| `thoughts` | list[str] | 每步的思考过程 |
| `history_resps` | list[str] | 模型每步的原始响应 |
| `urls` | list[str] | 每步所在页面 URL |

</details>

## ✅ 提交清单

需要包含：
- ✅ `config.json`
- ✅ `environment.yml`
- ✅ `scripts/run.sh`
- ✅ `src/` 目录

不要提交（已在 `.gitignore` 中）：
- ❌ `test_results/`
- ❌ `logs/`

## 📜 License

[MIT License](LICENSE)
