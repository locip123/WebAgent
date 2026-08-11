# WebRetriever Challenge Template

🔤 [中文](README_zh.md) | English

Submission template for the [WebRetriever Challenge](https://mininglamp-ai.github.io/WebRetriever_Challenge/), built on the [WebRetriever](https://github.com/Mininglamp-AI/WebRetriever) framework. Includes a complete [UI-TARS 1.5](https://github.com/bytedance/UI-TARS) reference Agent.

## ⚡ What You Need to Do

```
1. Clone this repo locally → git clone <your-team-repo-url>
2. Edit config.json → fill in your model API
3. Edit environment.yml → declare your runtime dependencies (conda + pip)
4. Customize your Agent (src/agent/) → implement your own logic + answer extraction
5. Push to the `main` branch (the evaluation system only reads `main` — do not create other branches)
```

> 💡 The challenge uses **Protocol III (End-to-End Task Protocol)**: navigating to the right page is **not enough** — you must extract the answer and write it to the `agent_answer` field in `result.json`. The template does **not** implement this step — **you must write it yourself**.

## ⚠️ Things to Note

| Item | Why |
|------|-----|
| **Must configure `config.json`** | Fill in your model API (see "Configuration" below); the Agent won't start without it |
| **Must configure `environment.yml`** | Declare your runtime env (conda + pip); the evaluation system installs deps from it |
| **`run.sh` interface is fixed** | The default script works as-is; you may add setup steps, but do not change the positional-argument interface (see "Evaluation Interface" below) |
| **Must fill `agent_answer`** | Scoring checks two things: ① did you **navigate to the target page** ② does `agent_answer` **semantically match** the ground truth. An empty field scores 0 |
| **≤ 100 steps per task** | A task over 100 steps is **scored 0** and skips semantic judging |
| **Don't change the sandbox connection** | `init_playwright_context` in `web_controller.py` is already wired to the remote sandbox; do not modify its connection logic (see "Evaluation Interface" below) |

## 📁 Directory Structure

```
├── config.json                # [Required] Model API configuration
├── environment.yml            # [Required] Environment deps (conda + pip in one file)
├── scripts/
│   └── run.sh                 # [Required] Evaluation entry script
├── src/
│   └── agent/
│       ├── main.py            # Multi-process task runner (usually no changes needed)
│       ├── agent.py           # Agent core logic (★ customize this)
│       ├── prompts.py         # Prompt templates (★ customize this)
│       └── web_controller.py  # Browser control (★ customize this)
├── data/
│   └── example_tasks.json     # Example tasks for local debugging
└── LICENSE
```

## 📝 Customizing Your Agent

`src/agent/` provides a reference implementation based on UI-TARS 1.5. You are free to modify or replace:

| File | What it does | What you can change |
|------|-------------|---------------------|
| `agent.py` | Model interaction, action parsing, screenshot processing | Replace with your own model/logic |
| `prompts.py` | System/user prompt templates | Redesign your prompt strategy |
| `web_controller.py` | Browser operations (click, type, scroll, screenshot) | Add browser capabilities; ⚠️ keep the sandbox connection logic in `init_playwright_context` |
| `main.py` | Multi-process scheduling, result saving | Usually no changes needed |

**💡 Using a different framework?** Absolutely — just put your framework code under `src/agent/`. Whatever framework you use, these three hard constraints must hold:

- **Fixed entry point**: use `scripts/run.sh` as the evaluation entry, compatible with the agreed positional arguments (see "Evaluation Interface" below).
- **Drive the browser via Playwright**: the remote sandbox browser must be connected and operated through Playwright (CDP); reuse `init_playwright_context` for the connection (do not modify its connection logic).
- **Fixed output paths**: for each task, write `result.json` (result), `trajectory/` (trajectory screenshots) and `capture.json` (captured network requests) under `{output_dir}/{task_idx}_{task_id}/`; producing `trajectory_visual/` (annotated screenshots, optional) is also recommended. See "Output Format" below.

### `agent_answer` — The Core Scoring Field

After navigating to the target page, your Agent must extract the answer and write it to the `agent_answer` field. The extraction method is up to you:

- OCR on screenshots
- DOM text parsing
- VLM visual Q&A
- Network response capture
- Or any combination

The template leaves `agent_answer` as an empty string — **filling it is your job**.

## ⚙️ Configuration (`config.json`)

> 💡 **Mind the concurrency**: for each team, the evaluation system provides up to **8** CDP URLs (i.e. up to 8 tasks running in parallel, all hitting your model at once) — make sure the model you configure here can handle this concurrency; self-deployed models can add instances via `api_ports` to share the load.

### Option A: Self-Deployed Model (vLLM / SGLang)

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

- `api_base`: public IP only (e.g., `http://203.0.113.1`).
- `api_ports`: one port per GPU; requests are round-robin distributed.

> ⚠️ **Must be a public IP.** The evaluation runs in cloud sandboxes. Verify from another machine: `curl http://YOUR_IP:8001/v1/models`

### Option B: Proprietary API (OpenAI / Claude / Qwen)

```json
{
    "api_base": "https://api.openai.com/v1",
    "api_key": "sk-xxx",
    "api_model": "gpt-4o"
}
```

- `api_base`: full URL **with `/v1`**, passed directly to the OpenAI SDK. No `api_ports` needed.

## 📦 Environment Dependencies (`environment.yml`)

The evaluation system creates your dedicated conda environment from this file (`conda env create -f environment.yml`), declaring both conda and pip dependencies in one place:

```yaml
dependencies:
  - python=3.10          # Python version
  # - nodejs             # declare system / other-language tools here (via conda-forge)
  - pip
  - pip:                 # declare Python packages here; no version = latest, pin with numpy==1.26.4
      - playwright
      - openai
      - ...
```

- **No version specified = install the latest**; to pin a version, write `package==version`.
- See the in-file comments for details; to add a dependency, append it under `pip:`.

## 🔌 Evaluation Interface (your `run.sh` MUST support this)

The evaluation system runs your `run.sh` **from your repo root**, inside the conda environment installed from your `environment.yml`, in this exact format:

```bash
bash scripts/run.sh <task_file> <output_dir> <cdp_url_1> <cdp_url_2> ...
```

| Parameter | Description |
|-----------|-------------|
| `task_file` | Path to the task JSON file for this run |
| `output_dir` | Result output directory (write each task to `output_dir/{task_idx}_{task_id}/result.json`) |
| `cdp_url_1 ...` | Browser CDP URLs — **variable count, up to 8** (= number of parallel workers available for this run) |

**Your `run.sh` must accept these positional arguments** (1st = task file, 2nd = output dir, 3rd onward = variable-length CDP URL list). The default `run.sh` passes them straight to `main.py`; you may add setup steps, but **do not change this interface**.

> 🌐 **The evaluation browser is a remote Tencent Cloud sandbox (not local), connected via Playwright.** Connection and auth are encapsulated in `init_playwright_context` in `web_controller.py`, which the template's `main.py` calls automatically on startup — **by default you don't need to handle the connection at all**; just write your click / type / scroll / parsing operations. But **do not modify the connection logic in `init_playwright_context`**, or the sandbox connection will fail during evaluation and every task will fail.

If you really need to open your own `connect_over_cdp` connection, be sure to attach the `X-Access-Token` header — just reuse `get_cdp_headers()` (`init_playwright_context` caches the token on its first connection, so you don't need to parse it yourself):

```python
from playwright.sync_api import sync_playwright
from web_controller import get_cdp_headers

p = sync_playwright().start()
# connect_over_cdp must carry the auth header, otherwise it can't connect to the sandbox
browser = p.chromium.connect_over_cdp(cdp_url, headers=get_cdp_headers())
```

## 📥 Input Format (`tasks.json`)

```json
[
  {
    "task_idx": 0,
    "task_id": "cdfae1f0...",
    "website": "https://example.com",
    "task": "Find the answer to..."
  }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `task_idx` | int | Task index (0-based) |
| `task_id` | string | Unique task ID |
| `website` | string | Starting URL |
| `task` | string | Natural language instruction |

> Some tasks may include `key_points` (scoring hints) — for reference only.

## 📤 Output Format (`result.json`)

Each task produces the following under `{output_dir}/{task_idx}_{task_id}/` — **keep these output paths consistent**:

- `result.json` — the task result (fields below); **only this file is scored**
- `trajectory/` — trajectory screenshots per step (`0.png`, `1.png` …)
- `capture.json` — captured page network requests
- `trajectory_visual/` — action-annotated screenshots (**optional, recommended**, useful for debugging and review)

Example `result.json`:

```json
{
    "task_idx": 0,
    "task_id": "cdfae1f0...",
    "task": "Find the movie ranked second...",
    "website": "https://example.com",
    "status": "SUCCESS",
    "reference_length": 100,
    "predict_length": 12,
    "agent_answer": "《731》",
    "final_result_response": "Located the target information...",
    "actions": ["click(100, 200)", "type('query')"],
    "thoughts": ["Analyzing page...", "Found target..."],
    "history_resps": ["...", "..."],
    "urls": ["https://...", "https://..."]
}
```

**Scoring rules:**
- Whether the Agent **successfully navigated to the target page**, and whether the value of `agent_answer` **semantically matches** the ground-truth answer.
- ⏱️ **Max 100 steps per task:** a task exceeding 100 steps is **scored 0 and skips semantic judging**.

<details>
<summary>Full field reference (click to expand)</summary>

| Field | Type | Description |
|-------|------|-------------|
| `task_idx` | int | Task index |
| `task_id` | string | Unique task ID |
| `task` | string | Task instruction |
| `website` | string | Starting URL |
| `status` | string | `SUCCESS` / `FAIL` / `FAIL_SCROLLDOWN` / `FAIL_SAVE_SCREENSHOT_ERROR` |
| `reference_length` | int | Reference step count |
| `predict_length` | int | Actual steps executed |
| `agent_answer` | string | **[Required]** Extracted answer — the core scoring field |
| `final_result_response` | string | Agent's final reasoning output |
| `actions` | list[str] | Action sequence per step |
| `thoughts` | list[str] | Reasoning per step |
| `history_resps` | list[str] | Raw model response per step |
| `urls` | list[str] | Page URL per step |

</details>

## ✅ Submission Checklist

Include:
- ✅ `config.json`
- ✅ `environment.yml`
- ✅ `scripts/run.sh`
- ✅ `src/` directory

Do not commit (in `.gitignore`):
- ❌ `test_results/`
- ❌ `logs/`

## 📜 License

[MIT License](LICENSE)
