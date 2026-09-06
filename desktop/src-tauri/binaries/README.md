# 旧的 sidecar 槽位（不再使用）

阶段 4 使用 PyInstaller `onedir`，不能只把单个可执行文件放入 `externalBin`：其动态 Python 资源和 Playwright browser 必须与可执行文件保持同一目录树。

发布资源由 `python -m browser_use.webretriever.desktop.release install-tauri-resources` 安装到 `../resources/sidecar/`，并通过 `bundle.resources` 纳入 Tauri 包。Linux 使用 `webretriever-sidecar`，Windows 使用同一资源槽中的 `webretriever-sidecar.exe`；开发态仍从活动 `Browser-Use` 环境启动固定 Python module。
