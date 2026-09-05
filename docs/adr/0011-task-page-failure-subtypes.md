# 任务页不可用保留故障族并增加稳定子类型

## 背景

`FAIL_BROWSER_TASK_PAGE_UNAVAILABLE` 只能说明任务页无法继续观察，不能区分运行时页指针丢失、页面对象已关闭、两级恢复耗尽或浏览器 target 崩溃。真实评测日志中这些症状还可能与截图超时、CDP 重连超时相邻出现。

## 决策

保留 `status=FAIL_BROWSER_TASK_PAGE_UNAVAILABLE` 作为兼容的故障族状态，在 `result.json.browser_failure` 增加稳定的 `subtype`。现有 `category` 继续用于粗粒度聚合，`error` 保留完整原始证据；恢复失败时额外写入 `recovery_stages`。

首批子类型包括：

- `task_page_missing`：运行时已启动但活动任务页指针为空。
- `task_page_closed`：活动任务页对象已关闭。
- `task_page_recovery_exhausted`：同一上下文重开与干净 worker 替换均失败。
- `target_crashed`：Playwright/CDP 报告目标崩溃。

其他浏览器故障（如 `no_live_page`、`download_page_orphaned`、截图超时、CDP 连接超时）也使用同一 `subtype` 字段，但保留各自的 `category`，避免把不同生命周期阶段误并为任务页缺失。

## 结果

下游可以继续按 `status` 或 `browser_failure.category` 做历史统计，并按 `subtype` 决定排障动作；不需要依赖 Playwright 版本相关的错误文本匹配。新增字段是向后兼容的，旧消费者忽略即可。
