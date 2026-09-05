# 只有执行器完成门禁可以写入 SUCCESS

WebRetriever 将模型的成功结束动作替换为不携带成功权力的答案候选：执行器先解析并校验所有 Evidence ID，再检查账本覆盖与答案契约，最后让不浏览网页的 Completion Verifier 依据已解析原子证据逐要求判定 `entailed`、`contradicted`、`insufficient` 或 `wrong_scope`；只有全部强制要求获得支持，执行器才写入 `SUCCESS`。拒绝结果转换成具体取证缺口并恢复浏览，而不是接受浏览 Agent 的自检结论；这增加了验证延迟和假阴性风险，但消除了非空答案与自由文本 evidence 直接升级为成功的路径。
