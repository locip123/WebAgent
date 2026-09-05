# 首个动作前的任务页缺失采用两级有界恢复

<!-- Status: superseded by ADR-0015 -->

当任务尚未执行模型或浏览器动作、但已启动的 `BrowserRuntime` 没有活动任务页时，执行器在同一 `BrowserContext` 中清理任务页并重开起始 URL（最多 60 秒）；失败后再以最多 160 秒重建干净 CDP 工作单元并重新观察。恢复成功不消耗步骤预算并要求模型重新定锚；两级恢复失败仍使用兼容的 `FAIL_BROWSER_TASK_PAGE_UNAVAILABLE`，同时隔离并恢复 worker。动作开始后不重放任务，以避免副作用重复。
