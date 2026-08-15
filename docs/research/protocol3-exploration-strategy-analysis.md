# Protocol III 探索策略：公开样本与现有轨迹分析

核验日期：2026-08-13。本文仅分析公开 `protocol3.json`、现有运行实现与本地历史轨迹；不把公开题答案、站点路径或网站特例作为隐藏赛题的硬编码知识。

## 结论

最值得优先实现的不是更多泛化的“思考/重试”，而是一个**任务相位驱动、按信息增益限额的探索控制器**：先判别任务为筛选检索、交互图表、文档表格还是排序/计算；在每个相位限定可用动作和失败后的下一模态；只有一次真实 UI 操作已产生相关首方 XHR/Fetch 时才进入网络数据路线。它直接针对当前轨迹中最昂贵的两类停滞：自定义表单/日期控件反复试错，以及对同一页面连续 `inspect_network` 改词检索。

公开题与正式题不重叠，故策略应是站点无关的能力层，而非公开网站 adapter 或答案记忆。[官方 Guide FAQ](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/) 明确说明正式 100 题与开源数据集题目互不重叠。

## 规则与成功判定

| 事实 | 对探索策略的含义 | 证据 |
| --- | --- | --- |
| Protocol III 要求从指定网站导航，并从文本、文档、图表等内容抽取结果。 | “到达页面”不是终点；策略必须明确进入 extraction/answer 阶段。 | [赛事主页](https://mininglamp-ai.github.io/WebRetriever_Challenge/)；[Guide](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/) |
| 单题通过条件是到达目标页且 `agent_answer` 语义正确；不要求字符串完全匹配。 | 结束前应由已验证字段生成简洁、任务语言一致的答案，不要只保留长证据或空答案。 | [Guide FAQ，输出格式与评分](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/) |
| 正式环境经 CDP 提供浏览器，所有浏览器交互必须经 Playwright；外部搜索引擎禁止，轨迹会核验。 | 合法的路线只有站内导航、页面可见交互、已观察到的首方链接/响应、下载和本地分析；不能把“找不到”交给外部搜索。 | [Guide，如何备赛](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/)；[Guide FAQ](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/) |
| 最多 8 并发、每个模型请求最多 3 分钟、每题最多 100 步且无重试。 | 行动的机会成本很高；应在低信息增益循环出现时尽早切换路线，不能靠无限 probe 或重跑。 | [Guide FAQ](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/) |

本仓库入口只是 `run_webretriever.py -> browser_use.webretriever.cli.main`；Runner 同样把上限限制为 100 步（`browser_use/webretriever/runner.py`），默认单题 watchdog 为 600 秒。公开 Guide 未公布总任务时限，因此策略不能依赖本地某次 900/1200 秒试跑的余量。

## 公开数据反映的任务形态

`data/data/protocol3.json` 恰有 100 条记录、98 个不同起始网站。下面数字是按任务文本关键词的可复核近似统计，类别可重叠，目的是决定能力优先级而不是声称官方标注。

| 任务信号 | 数量 | 典型索引 | 策略要求 |
| --- | ---: | --- | --- |
| 明确选择/筛选/搜索/输入/切换等交互 | 50 | 50–76、80–97 | 自定义下拉、日期、输入、结果提交与筛选回读必须稳定。 |
| 图表/hover/数据点/走势 | 15 | 61、63、65、67、69–75、99 | 筛选状态 → tooltip/表格/首方 chart XHR → 必要时本地计算。 |
| 报告、文档、PDF、Excel、表格 | 13 | 6、15、16、21、23、30、43 | 文档定位、下载内容验证和表头/单位阅读。 |
| 排名/极值/top-N | 28 | 0、3、5、10、17、77、79–84 | 处理分页、排序方向、全局/当前页、并列和单位。 |
| 总数、总和、增速、比例、平均等派生/聚合 | 28 | 1、3、5、35、45、50、77、97 | 先枚举与核验全部 operand，再计算。 |
| 多约束文本（含多筛选、日期范围、多个输出字段） | 68 | 6、7、14、50–76、84–99 | 将约束变成可回读的 checklist，禁止“看似已设置”即提取。 |

结构上，索引 50–76 几乎是一段连续的仪表盘/数据浏览器/图表读数任务；77–97 则高度集中于高级搜索、结果计数、排名和表单。隐藏题不重叠，但这说明应优先投资两条通用路径：**filter-to-result** 与 **filter-to-chart-value**。

## 现有实现已具备的基础

- Prompt 已要求逐个应用并核验筛选、覆盖分页/虚拟列表/排序、在图表上读取 tooltip，并可使用已捕获的首方 chart 流量：`browser_use/webretriever/prompts.py` 第 136–142 行。
- 有 document/chart/derived 三类条件 playbook，且支持 `find_chart_data_requests`、`call_data_analysis_assistant` 和本地 `calculate`：`prompts.py` 第 643–741 行、`models.py` 第 29–105 行。
- 有探索 checkpoint、相同动作/振荡/连续 probe 检测：`strategy.py`；`agent.py` 第 87–185、1060–1131 行。
- 有可见验证状态机，位于普通 LLM 决策之前，能够在同一个 Playwright context 内点击控件、等待、确认恢复：`verification.py` 第 67–282 行；`agent.py` 第 760–822 行。

这些基础方向是正确的；缺口主要在“何时选哪一条路线、如何判定该路线已不再带来新信息”。

## 历史轨迹暴露的探索瓶颈

历史输出不是正式成绩，也包含重复运行，不能拿来估计成功率；但它们是具体的失败形态证据。

1. **表单操作没有形成字段状态机。**
   - 任务 62（BTS）在步骤 17/18 已成功选中 BOS 和 JetBlue，之后仍反复滚动、点击、查找提交与结果，至 58 步超时。见 `outputs/protocol3_8/62_FAIL_TASK_TIMEOUT/result.json`。
   - 任务 95（HKEX）先对本应点击展开的自定义 category 控件使用 `select`，随后在日期控件上反复 `type/click/drag`，第 38 步超时；最后记录的 From/To 仍非任务范围。见 `outputs/protocol3_9/95_de7ae5ba9cc249ffa3512eb016152fe8/result.json`。
   - 说明“点击成功”不是“字段值已生效”。需要面向控件类型的操作及显式 read-back。

2. **网络检索被当作无界的主探索路线。**
   - 任务 77（KPL）在首个有效 UI 路线尚未建立前，连续以 `hero`、`winrate`、`heroid` 等词搜索网络；即使 loop guard 告警，后续仍在新页面再次成组检索，58 步超时。见 `outputs/protocol3_9/77_1/result.json`。
   - `agent.py` 的 `_action_intent()` 将所有 `inspect_network` 归为同一 intent（第 96–114 行），连续 6 次就阻断（第 155–168 行），但阻断后又清空最近签名（第 1096–1130 行）。这会同时造成两种问题：有价值的分页/指定 request 读取可能被过早截断；无价值的不同词查询又能在清空后重新开始。

3. **checkpoint 是叙述性计划而不是受约束的路线管理。**
   当前 checkpoint 在首屏、每个不同 URL 页面和每 20 个决策上要求模型提交完整目录（`strategy.py` 第 142–259 行）。它能让模型看到循环，但不保存“某子目标在某一模态已经没有新证据”的可执行预算，也不约束下一动作。因此复杂任务仍会回到已失败的表单/网络路径。

4. **图表数据的筛选 provenance 不足。**
   Prompt 要求先验证筛选；但 `ChartNetworkInspector._create_scan()` 保存 artifact 时仅将 `page_title` 写入 `active_filters`（`network.py` 第 669–707 行）。后续年份冲突检测依赖这个字段（`agent.py` 第 578–599 行），却拿不到用户刚刚在 UI 中确认的国家、类别、时间等实际值。动态 dashboard 很容易读取到旧默认请求或相邻图表数据。

## 最优先的探索改进

### 1. 增加 Filtered Retrieval / Form playbook（最高优先级）

现有条件 playbook 覆盖 document/chart/derived，但没有涵盖公开样本最大的一类筛选检索。建议在任务初始解析后建立不可变 `ConstraintLedger`：

```text
每项约束 = {字段、目标值、依赖字段、UI 证据、已确认/未确认}
阶段 = DISCOVER → SET_FILTERS → VERIFY_FILTERS → SUBMIT/WAIT → EXTRACT → COMPUTE → ANSWER
```

- 根据 DOM 能力选择动作：原生 `<select>` 才使用 `select`；combobox/listbox 采用“点击展开 → 输入/选择精确选项 → 读回标签”；日期控件采用“清空 → 输入标准值 → blur/Enter → 读回 value”，一次失败后切换为可见日历，不允许无依据的反复拖动。
- 每一个写入动作后只允许一次高价值 read-back（value、已选 chips、URL/请求参数、结果标题）。未确认的上游字段不得提交，也不得进入提取。
- 把“提交后发生了哪些预期变化”（结果计数、表格标题、XHR、URL）作为转移条件；缺少变化时进入一次控件诊断，而不是滚动/重输。
- 这个 playbook 同时适用于高级搜索、榜单排序、数据门户和邮资计算器，远比对某个公开网站写特例更适合隐藏题。

### 2. 用“子目标 × 模态 × 新证据”管理探索预算

以当前 raw action loop guard 为基础，改成可执行的 route ledger，而非只输出自然语言 checkpoint。

```text
subgoal: 找到目标数据载体 / 设置国家字段 / 获取结果计数 / 读取 2019 数据点
modality: DOM-UI / 页面文本-表格 / 触发后的首方网络 / 下载-文档 / 视觉-tooltip
progress: 筛选证据变更、URL/请求参数变更、新 request_id、新表头/行/tooltip、新可计算 operand
```

- 同一 `(页面世代, 子目标, 模态)` 两次都没有新证据，就禁止第三次同类 probe，强制进入预定义的下一模态。
- 对网络，只有 UI 已经触发相关请求、且现有观察显示 request URL/方法/时间与子目标相关时才允许；先看具体 request，再允许有限 cursor 翻页。不要用连续同义词 `inspect_network(text=...)` 猜字段名。
- 对 DOM/text，找不到两次后应转向已经观察到的站内链接、导出/下载、表格 tab 或图表 tooltip；对 tooltip，坐标尝试也应按候选点/时间轴证据限额。
- 若确实获得新 request ID、分页 cursor 或新筛选状态，则预算重置；这避免当前“所有 `inspect_network` 一律被第六次阻断”的误伤，也避免 clear 后的重复六连发。

### 3. 把图表路线变成“最终筛选后的一次定向提取”

- 任务分类除关键词外，还应利用 DOM/canvas/SVG/Highcharts/Plotly、图例、时间轴、当前 XHR 内容等信号；不少任务只说“某年数值”或“值”，未必包含“图表/hover”。
- 在 `VERIFY_FILTERS` 成功时，把已回读的完整 `ConstraintLedger`、当前 URL、document generation、触发动作及其之后的 request IDs 一起写入 chart artifact；不能只写页面标题。
- 优先级为：可见表格/精确 tooltip → 筛选后新增的首方 XHR/Fetch → 首方下载/静态图；网络归一化与数据分析是昂贵路径，且当前会为它们保留 60+90+30 秒（`agent.py` 第 50–52、530–537 行），应避免对未筛选页面扫描。
- 读取 chart response 后再次校验 series、单位、时间、地理/类别和筛选回执，才允许计算/finish。

### 4. 让 replan 由语义事件触发，而不是主要按 URL/20 步触发

保留首次计划和页面进入时的概览，但把周期 checkpoint 替换/补充为以下硬事件：

- 一个字段写入后未通过 read-back；
- 提交后页面、URL、结果和网络都未出现预期变化；
- 当前子目标的某一模态两次无新证据；
- 图表/下载已就绪、需要从“探索”转入“提取”；
- 剩余步数/时间低于保留阈值。

checkpoint 内容应收缩为结构化 route ledger（已验证约束、当前相位、每条路线预算、下一最高信息增益动作），而非主要让模型重述所有可能策略。这样既省 token，也让行动选择可被执行层真正约束。

### 5. 验证/风控：让“可见验证交互”抢占探索，但不要把所有拒绝都当 CAPTCHA

用户给出的赛事说明明确把要求人工点击的真实网站验证视为环境的一部分。合规且有效的行为是：在官方 CDP 的同一 Playwright context 内识别可见控件，正常点击、等待验证处理、确认原目标页恢复，并完整保留截图和动作；不是换浏览器身份、外部代解、token 注入或搜索引擎。

当前 `VerificationController` 已正确放在 LLM 前，并限制为至多 2 次点击、10 次 3 秒等待（`verification.py` 第 77–216 行）。建议补强为：

- 识别信号从英文正文扩展到可见 iframe provider URL、ARIA/role、checkbox 状态、常见中文验证文本及截图视觉候选；`ElementRef` 已保留 `frame_url`/`signals`，但 `render_text()` 并未展示它们（`browser.py` 第 245–285 行）。
- 严格区分三态：`visible_challenge`（正常点击/等待）、`rate_limited_or_access_denied`（限频、保留上下文、一次低频首方恢复/站内回退）、`network_or_blank_failure`（有限恢复）。后两者不能被误点 CAPTCHA 控件，也不能发展为无界刷新。
- 等待预算以可观测进展为依据：checkbox 已勾选、标题/URL/DOM 变化、cookie/页面恢复时延长确认；完全静止才耗尽预算。验证处理中禁止 `back`、reload 或普通网络探针，以免打断挑战。

## 建议的实施顺序与验收

1. **先做 Form/ConstraintLedger + action read-back。** 复跑公开的 62、95 类轨迹，验收条件是每项约束都有明确回读，且同一日期/combobox 不出现三次以上无状态变化动作。
2. **再做 route ledger / evidence-aware probe budget。** 使用公开 77 类任务验收：网络检索必须有 UI 触发证据；不得出现“六次 probe 被挡、清空后又六次”的循环；有新的指定 request/cursor 时不应被误拦。
3. **补齐 chart filter provenance 和 chart router。** 使用 61–76 中的 dashboard 题复测，artifact 必须包含所有已验证筛选值及对应页面世代/请求；结果必须回查单位、series 和年份。
4. **最后完善可见验证状态机与回归。** 模拟/回放 challenge、rate limit、Access Denied、空白页四类观察；确保只有前者触发可见控件点击，且处理期间不被普通 LLM 动作打断。

每项都应在现有轨迹目录和 `result.json` 中留下可审计证据。不要以公开答案是否碰巧正确作为唯一指标；应同时记录约束完成率、首次到达 answer-bearing surface 的步数、每子目标无信息动作数、模态切换原因和验证状态转移。

## 来源

- 公开任务与样本答案（答案仅供离线测评，不能传给 agent）：`data/data/protocol3.json`。
- 数据集协议说明：`data/README.md` 第 68–76 行。
- 正式规则与评分：[WebRetriever Challenge Guide](https://mininglamp-ai.github.io/WebRetriever_Challenge/guide/)；特别是 Playwright/CDP、无搜索、限额、输出与语义评分 FAQ。
- 当前 prompt：`browser_use/webretriever/prompts.py`。
- 探索控制：`browser_use/webretriever/strategy.py`、`browser_use/webretriever/agent.py`。
- 网络和图表 artifact：`browser_use/webretriever/network.py`。
- 验证状态机：`browser_use/webretriever/verification.py`。
- 具体历史轨迹：`outputs/protocol3_8/62_FAIL_TASK_TIMEOUT/result.json`、`outputs/protocol3_9/77_1/result.json`、`outputs/protocol3_9/95_de7ae5ba9cc249ffa3512eb016152fe8/result.json`。
