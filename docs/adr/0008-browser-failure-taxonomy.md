# 细分浏览器任务终态并保留结构化诊断

浏览器任务不再将运行时未启动、任务页丢失、CDP 执行单元不可用、会话断开与观测/启动异常统一写为 `FAIL_BROWSER`。我们改用稳定的 `FAIL_BROWSER_*` 终态，并在 `result.json` 中写入不包含易变错误文本的 `browser_failure` 对象；这样既保留 `FAIL_` 失败汇总兼容性，也让排障和根因统计不依赖 Playwright 的具体报错措辞。探索路径初始化是协议错误，改用 `FAIL_EXPLORATION_PATH_INITIALIZATION`，不再误导为浏览器故障。
