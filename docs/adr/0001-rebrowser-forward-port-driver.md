# 使用显式 Rebrowser 前移补丁驱动

WebRetriever 保持 Playwright 与 Patchright 两条既有路径，并将 Rebrowser 作为显式选择的第三个实验驱动。由于 `rebrowser-playwright` 尚未提供与项目 Playwright 1.61 匹配的发行包，Rebrowser 路径只在目标 Node driver 的版本和哈希均获验证时应用前移补丁；不匹配则拒绝启动，既不降级依赖也不回退为未补丁的客户端，从而使对照实验可归因且不污染基线。
