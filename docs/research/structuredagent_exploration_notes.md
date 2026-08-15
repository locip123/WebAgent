# StructuredAgent：对 WebRetriever 探索策略的可迁移研究笔记

日期：2026-08-13

研究对象：[《StructuredAgent: Planning with AND/OR Trees for Long-Horizon Web Tasks》](../../STRUCTUREDAGENT.pdf)。下文“论文第 *n* 页”均指该 PDF 阅读器的页码，并同时标出章节；论文结论是作者在 WebVoyager、WebArena 与复杂购物任务上的实验结果，不应直接当作 Protocol III 的已验证收益。

## 结论先行

WebRetriever 已经具备这篇论文最重要的两个雏形：

- 它有持久的探索检查点（策略目录、当前策略、已证伪策略、下一策略）；
- 它能识别重复动作、探测循环，并要求切换信息获取模态。

真正缺少的不是“再写一段要求模型多想的 prompt”，而是让运行时拥有一个**小而确定的探索状态机**：把任务的必达约束、候选取证、策略分支、失败原因和依赖关系存成结构化状态。模型只负责局部的“展开 / 选择 / 修复”建议，运行时负责状态转换、预算、剪枝和完成判定。这是论文最值得借鉴、也最适合当前架构的部分。

不建议把论文的完整 AND/OR 树和每节点 LLM 调用原样搬进比赛路径。Protocol III 有 100 步与总时限约束；论文自己也观察到更充分的动态重规划会拉长困难任务的轨迹和完成时间分布。[论文第 11 页，§4.1](../../STRUCTUREDAGENT.pdf)；[第 14 页，Fig. 9](../../STRUCTUREDAGENT.pdf)。更合适的是“**按需、浅层、可审计的 AND/OR 恢复图**”。

## 论文机制及其证据

| 机制 | 论文中实际做法 | 对 WebRetriever 的含义 |
| --- | --- | --- |
| AND/OR 分解 | `AND` 表示必须共同完成的子目标，`OR` 表示可替代策略，`ACTION` 是单个浏览器操作。AND 可以表达顺序依赖，OR 在任一子项成功后即成功。[论文第 4 页，§3.1](../../STRUCTUREDAGENT.pdf) | 把“找对页面”和“拿到所有请求字段”拆开。日期、地域、筛选器、指标、排名范围、单位、输出字段、证据各自是 AND 约束；站内搜索、表格、图表 tooltip、下载、已观察到的第一方请求则是 OR 路径。 |
| 在线、延迟展开 | 从根节点做修改版贪心 DFS；遇到 AND 按顺序处理，遇到 OR 只先试成功概率最高的未处理分支，行动后再根据新观察修改树，而不是一开始生成一个静态长计划。[论文第 4–7 页，§3.2–3.3](../../STRUCTUREDAGENT.pdf) | 不必先规划 100 步。只在当前未解决的“关键不确定性”上展开 2–4 个不同模态的路径，并保留未尝试分支，避免模型在一次失败后忘记替代路线。 |
| 框架和模型分工 | 树的构造、遍历、维护由框架承担；LLM 只做局部节点展开、修复、完成检查、全局更新和观察摘要等受限操作。[论文第 2–3 页，§1–2](../../STRUCTUREDAGENT.pdf)；[第 6 页，§3.3](../../STRUCTUREDAGENT.pdf) | 将“是否已尝试、能否重试、父子依赖、何时剪枝、是否允许 finish”从自然语言记忆中移出，放进 Python 数据模型；prompt 只请求一个局部决策，降低长上下文漂移。 |
| 可重入的节点状态 | 节点可反复进入 `ENTERING`、`EXITING`、`FAILED`。这比传统 DFS 的 visited/unvisited 能表示行动后的重规划、剪枝和故障向上传播。[论文第 5–6 页，§3.2–3.3](../../STRUCTUREDAGENT.pdf) | 当前动作“未报错”不等于子目标完成。应区分 `pending / executing / verified / failed_transient / exhausted / pruned`，并在动作后检查页面或证据是否真的推进了对应子目标。 |
| 有界修复和剪枝 | ACTION 失败后会剪枝；AND 子项失败时删除其后续、依赖它的顺序兄弟；OR 优先试下一个分支，全部耗尽后才在修复预算内新增路径，最终才向上失败。[论文第 7–8 页，§3.3](../../STRUCTUREDAGENT.pdf)；[第 18–24 页，§D.1 与 Algorithms 4/9/10](../../STRUCTUREDAGENT.pdf) | “loop_detected”之后不能只给模型一句“换模态”。运行时应把这条路线标为 exhausted，选择尚未试过的 OR 分支；若一个前置筛选失败，则自动使依赖该筛选结果的后续提取计划失效，但保留已核验的事实。 |
| 回滚以隔离分支 | 处理 OR 子分支前恢复父节点 URL；内部函数表将其定义为恢复浏览器上下文的 rollback。[论文第 18 页，Algorithm 2](../../STRUCTUREDAGENT.pdf)；[第 25 页，Table 7](../../STRUCTUREDAGENT.pdf) | 每条候选路线应可选择性地从已知基线恢复，而非在一个已污染的筛选/弹窗/分页状态上继续试错。对有状态表单须谨慎：只保存并验证 URL、tab、标题、查询和可见筛选器，不能假设 URL 足以恢复 UI 状态。 |
| 结构化候选记忆 | 对信息检索任务，把候选实体作为表的行、约束/属性作为列；浏览中增删改候选，规划/修复时取满足约束最多的 top-K 以避免重访与遗漏。[论文第 8 页，§3.4](../../STRUCTUREDAGENT.pdf) | 对多记录、多年份、top-N、最值和多筛选任务，应维护“候选证据表”，而非仅用一段文本 ledger。对单个标量问题则不启用，避免状态噪声。 |
| 根级完成检查 | 作者发现每个 AND 节点都做严格完成检查“过于严格”，实现中只对根节点使用该严格检查。[论文第 18 页，§D.1](../../STRUCTUREDAGENT.pdf) | 完成门应放在任务根部：所有请求字段和约束有可追溯证据才允许 `finish(success=true)`；中间节点采用更轻的语义进展检查，避免因中间计划写得不完美而卡死。 |

论文的 Structured Memory 在复杂、多候选购物任务的人评中带来约 5 个百分点增益，但在简单 Amazon/WebVoyager 任务上略有下降，作者将其归因于过严的中间约束成为噪声。[论文第 10 页，§4.1](../../STRUCTUREDAGENT.pdf)。因此，记忆表应是**任务形态触发的工具**，不是所有任务必开功能。

## 当前 WebRetriever 的基线：已有优势与真实缺口

当前实现并非从零开始。

| 当前能力 | 已有价值 | 与论文相比的缺口 |
| --- | --- | --- |
| 文本 ledger | 系统 prompt 已固定 `Constraints / Verified / Candidates / Tried-Blocked / Next`，并限制为 3,000 字符；这比纯历史回放更接近任务记忆。[prompts.py:136–149](../../browser_use/webretriever/prompts.py) | 字段仍是模型自由文本，不能稳定地去重、按约束检查、关联来源、记录反证或让运行时判断依赖失效。 |
| 探索检查点 | `ExplorationCheckpointTracker` 持久保存策略目录、当前策略、已确认不可行与下一策略，并在初始页、页面切换、每 20 个决策触发复盘。[strategy.py:17–18](../../browser_use/webretriever/strategy.py)；[strategy.py:142–255](../../browser_use/webretriever/strategy.py) | 这相当于扁平 OR 策略目录，但没有分支 ID、排序分数、前置条件、状态快照、失败类别、重试/修复预算或父子依赖。页面切换/固定间隔也不是最有信息量的重规划时机。 |
| 循环防护 | 运行时能检测完全重复动作、A/B 振荡、连续无效探测和小状态空间里的意图抖动；检测后阻止该动作并要求换模态。[agent.py:87–181](../../browser_use/webretriever/agent.py)；[agent.py:1060–1131](../../browser_use/webretriever/agent.py) | 检测结果没有转化为结构化分支状态；模型仍需从 outcome 文本自行推断哪条策略死了、什么替代路径尚可用。 |
| 语义化恢复建议 | prompt 已明确：页面文本、网络、元素读取都失败时改走截图/导出/下载；不要反复 probe。[prompts.py:136–142](../../browser_use/webretriever/prompts.py) | 这是正确的策略规则，但缺少“同一目标上每种模态尝试过几次、证据是什么、下一条优先级为何”的运行时账本。 |
| 验证状态机 | `VerificationController` 对可见验证做一次或有限次数的点击、等待和恢复确认，超过预算熔断；它不换浏览器、不改身份，也保留轨迹。[verification.py:67–216](../../browser_use/webretriever/verification.py) | 验证操作在主探索循环之外直接 `continue`，不会作为普通探索决策进入检查点窗口。[agent.py:776–822](../../browser_use/webretriever/agent.py) 因而后续规划看不到“验证已尝试几次、是否已通过/阻断、可否安全恢复原路线”的结构化因果状态。 |
| finish 基线 | prompt 要求所有约束和字段均已 grounded，成功 finish 必须带 answer 与 evidence。[prompts.py:140–149](../../browser_use/webretriever/prompts.py)；[models.py:413–418](../../browser_use/webretriever/models.py) | 运行时主要校验 schema 非空，逐个任务约束是否都有证据仍由模型自检；缺少可以阻止“找到文档就提前结束”的确定性 coverage 表。 |

## 建议的最小可迁移设计

### 1. 用浅层 `ExplorationState` 补强现有 checkpoint

不要让模型输出完整树。由运行时维护一个最多几十个节点的浅图即可，节点是“尚未解决的不确定性”而不是每一次 click：

```text
Root：所有必答字段均有证据？
 ├─ AND 约束：目标实体 / 时间或范围 / 筛选状态 / 指标定义 / 数值或排名 / 单位 / 证据
 │    └─ 每一项都有状态：unknown | verified | contradicted | blocked
 └─ OR 路线：站内表格 | 已观察图表请求 | 页面 tooltip/图片 | 第一方导出 | 已观察的第一方文档
      └─ 每一项都有状态：untried | active | succeeded | transient_failure | exhausted | pruned
```

建议的节点字段：`id`、`kind`（requirement/route/action）、`description`、`parent_id`、`depends_on`、`status`、`attempt_count`、`revision_count`、`priority`、`failure_class`、`snapshot_id`、`evidence_refs`。其中 `evidence_refs` 指向 URL、页面标题、表行/字段、筛选器、请求 ID 或下载文件，而不是复制大段网页文本。

这直接对应论文的“框架维护树、LLM 做局部操作”的分工；但限制深度和节点数，避免在 100 步比赛中把 token 与时间花在树本身。[论文第 2 页，§1](../../STRUCTUREDAGENT.pdf)；[第 6 页，§3.3](../../STRUCTUREDAGENT.pdf)。

### 2. 将“策略目录”升级为可执行的 OR 分支选择

现有 checkpoint 已要求列出“材料上不同”的策略，且明确不把改写 query 或换 element ID 当成新策略，这是很好的基础。[prompts.py:829–836](../../browser_use/webretriever/prompts.py)。建议补充以下规则：

1. 每个策略类必须绑定一个 `route_id` 和一个明确的**目标不确定性**，例如“读取 2025 年筛选后的完整排名”，而不是“再试一下网络”。
2. 运行时根据 `可行性 × 预期信息增益 ÷ 预计动作成本` 排序；模型可建议排序，但不能重置已耗尽路线。这个效用函数是结合论文的 OR 成功可能性排序与比赛预算得出的工程推论，不是论文的原始公式。
3. `loop_detected`、`repeated_unchanged_action`、同目标的多模态失败、明显的 403/429、验证熔断，都要写成 `failure_class`；只有暂态加载/陈旧元素保留一次有限重试。
4. 触发重规划改为“语义事件优先”：关键筛选生效/被重置、目标页面进入、路线失败、循环、验证状态改变、证据覆盖发生变化；20 步周期可保留为兜底，不应是主触发器。

这实现了论文的 OR 分支“先试最佳未处理项、失败后试下一项、耗尽后有限修复”的核心，而不需要完整 DFS。[论文第 6–7 页，§3.3](../../STRUCTUREDAGENT.pdf)；[第 22、24 页，Algorithms 8、10](../../STRUCTUREDAGENT.pdf)。

### 3. 用“候选证据表”替换高风险的自由文本候选区

建议把当前 memory 中的 `Candidates` 在复杂检索任务下渲染为紧凑表；表不只适用于商品，应该适用于事实候选：

| candidate | 已满足/待验证约束 | 精确值或反证 | 来源与定位 | 状态 |
| --- | --- | --- | --- | --- |
| 某统计表/记录/年份/地区 | 年份、地区、指标、单位 | 只写可见原值 | URL + 标题 + 行/列/筛选/请求 ID | `candidate` / `verified` / `rejected` |

具体要求：

- 只收录浏览器可见或任务轨迹已产生的数据；缺失字段留空并标记 `missing`，绝不以推断补齐。
- 用“实体 + 时间/版本 + 来源”去重；后续页面补到字段时更新同一候选。
- `rejected` 必须保留反证和来源，防止回到已经排除的页面；但不应把暂态错误当作反证。
- 仅在任务有多个候选、比较/排序、多个筛选条件或需要跨页拼接时启用。单一字段查找继续使用轻量 `Verified` ledger。

这保留论文结构化记忆“候选 × 约束”的优势，同时避开其对简单任务造成额外噪声的已知风险。[论文第 8 页，§3.4](../../STRUCTUREDAGENT.pdf)；[第 10 页，§4.1](../../STRUCTUREDAGENT.pdf)。

### 4. 让动作成功必须带“子目标进展”

将 browser action 的技术成功（无异常、点击已发出）与规划成功（是否推进目标）拆开：

- `executed`：动作已由 Playwright 发出；
- `effect_observed`：URL、标题、可见筛选 chip、表头/行、下载、网络响应或目标元素至少有一个预期变化；
- `requirement_verified`：从变化中取得了某个请求字段的可追溯事实。

只有最后一种状态才能关闭某个约束节点。若点击成功但筛选未变，则把它记为该路线的“无语义进展”，走修复或下一 OR 路线，不要重新把同一 click 当作新探索。论文在 ACTION 后更新观察摘要和全局树，并在退出节点时检查目标完成，正是这个差别。[论文第 6–7 页，§3.3](../../STRUCTUREDAGENT.pdf)；[第 21 页，Algorithm 6](../../STRUCTUREDAGENT.pdf)。

### 5. 在根部增加确定性的 coverage gate

从任务解析出稳定的 requirement ID，例如：`entity`、`period`、`geography`、`filter`、`metric_definition`、`rank_scope`、`result_value`、`unit`、`requested_output_format`。每项都应有 `unknown / verified / contradicted / unavailable` 及至少一个 evidence reference。

- `finish(success=true)` 前：所有必填项必须是 `verified`，且答案引用的每一个数值/比较都有来源；
- 如果发生时间耗尽：可使用现有 partial salvage，但输出应明确是未完成结果，不能把 `verified` 的局部内容伪装为完成；
- 中间子目标不做过严的全局完整性判定，只检查必要前置条件，和论文将严格 completion check 限制在根节点的实践一致。[论文第 18 页，§D.1](../../STRUCTUREDAGENT.pdf)。

这会把当前 prompt 中正确但纯语言层的“不能仅因找到页面就结束”变成可验证的控制逻辑。[prompts.py:140–142](../../browser_use/webretriever/prompts.py)。

### 6. 把验证/风控当作独立失败分支，而不是普通网页噪声

当前有界、可审计的 `VerificationController` 是应保留的安全边界。迁移论文思想时，不应增加点击次数、更换浏览器身份、使用代理或外部解题服务；只需让探索状态认识到以下事实：

- `verification_pending`：暂停依赖该页的普通路线，保留当前任务/来源/基线快照；
- `verification_passed`：先验证原子任务页面是否已恢复，再恢复原路线；
- `verification_blocked`：标记此路线因访问状态受阻。仅当浏览器轨迹已观察到另一条合法、第一方、非猜测的路线时，才把它作为 OR 候选；否则停止重复点击/等待。

这样既符合论文的“失败向上传播而非重复行动”，也保持比赛所要求的 Playwright 内可审计验证处理。[论文第 7–8 页，§3.3](../../STRUCTUREDAGENT.pdf)；[verification.py:67–216](../../browser_use/webretriever/verification.py)。

## 推荐实施顺序与实验

### P0：低风险、先验证收益

1. 将 `Tried-Blocked` 规范成失败类别 + 目标不确定性 + 已尝试模态 + 证据，而不是一段叙述。
2. 在策略检查点中要求每条路线显式对应一个未满足 requirement；触发条件加入 loop、筛选失效、403/429、验证状态改变。
3. 在 prompt 中提供根级 coverage matrix；finish 前让模型回填每一项的 evidence ref。

这一阶段可以只改状态表示与 prompt，不要求多一次模型调用。

### P1：运行时浅层恢复图

1. 在 `strategy.py` 附近引入可序列化的 `ExplorationState` / `RouteState`，由 runner/agent 维护状态而非让模型全文重写。
2. 将现有循环检测和动作 outcome 映射到统一 `failure_class`，自动把耗尽的 `route_id` 排除出下一次选择。
3. 增加 `effect_observed` 的轻量断言（URL/可见筛选器/目标文本/网络下载的变化），并记录到轨迹。

### P2：只为复杂事实题启用候选证据表

先以“top-N、最大/最小、跨年比较、多条记录、多个独立筛选器”作为启用条件；比较关闭/开启时的成功率、错误提前 finish 率和平均步数。不要将商品论文中“满足 60% 才入表”的阈值照搬到统计、档案、法规类问题。

### 应记录的实验指标

- 根级 coverage 全满后才 finish 的比例，以及成功答案中无证据字段的比例；
- 触发循环后，下一次动作是否真的进入未尝试模态/路线；
- OR 分支恢复成功率、每条路线的平均成本、耗尽路线被重复选择的次数；
- 复杂任务的成功率与 P50/P95 步数、模型调用时间；
- 候选表开启与关闭分别在简单标量题、复杂多约束题上的效果；
- 可见验证的通过率、熔断次数、熔断后是否发生不合规或无意义的重复操作。

## 不应照搬的部分

1. **每个节点都调用模型。** 论文有多个 LLM 操作器（展开、修复、摘要、完成检查、全局更新），这对其研究框架合理，但对比赛时延和 token 预算偏重；先让确定性运行时处理状态，LLM 只在分支展开/修复触发时介入。[论文第 6 页，§3.3](../../STRUCTUREDAGENT.pdf)。
2. **固定静态长计划。** 论文的价值恰恰是观察后更新、剪枝和修复；WebRetriever 的计划也应延迟展开，不能在初始页凭空列出未观察到的端点或导航路径。[论文第 4–5 页，§3.2](../../STRUCTUREDAGENT.pdf)。
3. **将失败一律上升为任务失败。** 临时加载、陈旧 element、筛选被重置、网络解析失败、页面像素化、访问验证阻断的恢复方式不同；必须按失败类型处理。
4. **将结构化记忆全局强制开启。** 论文已经显示它在简单任务上会带来噪声。[论文第 10 页，§4.1](../../STRUCTUREDAGENT.pdf)。
5. **用回退当作绕过。** OR 分支只能是从起始站点、已观察链接/请求或官方文档中得到的合法第一方路线；它不是猜 URL、外部搜索、外部 HTTP、代理、指纹修改或 CAPTCHA 代解的借口。

## 最终判断

最有价值的借鉴是：把当前“checkpoint + 文本 memory + loop prompt”升级为**运行时拥有分支状态和约束覆盖证据的浅层规划器**。这样能把一次循环/失败从“模型记得要换方法”变成“系统知道哪条路径已经耗尽、哪条路径仍可试、哪些事实仍有效、何时允许结束”。

对当前 WebRetriever，优先级应是：`根级 coverage gate` → `失败分类与 OR 路线状态` → `复杂题候选证据表` → `选择性快照/回滚`。完整 AND/OR 树可作为后续消融实验，而不应成为第一版实现的前置条件。
