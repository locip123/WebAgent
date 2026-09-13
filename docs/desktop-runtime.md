# 本地桌面运行时基线

桌面 v1 支持 Linux GNU x64、Windows x64 (`x86_64-pc-windows-msvc`)、macOS Intel (`x86_64-apple-darwin`) 和 Apple Silicon (`aarch64-apple-darwin`) 发布包。Python Runner 的工件锁使用跨平台 `portalocker`；Windows desktop supervisor 会将 sidecar 放入带 `KILL_ON_JOB_CLOSE` 的 Job Object，确保强制退出或桌面进程意外退出时清理 sidecar 子进程树。

开发和 sidecar 基线为 Python 3.12。创建或更新环境后，使用以下命令安装与仓库声明的 Playwright 版本匹配的 Chromium：

```bash
conda env create -f environment.yml
conda activate webAgent
python -m playwright install chromium
```

已存在的 `webAgent` 环境可跳过创建步骤，但必须确认 `python --version` 是 3.12。发布包不要求最终用户安装 conda：它将随 sidecar 一起交付由已固定 Playwright revision 驱动的 Chromium；该发布打包工作属于阶段 4。

Windows PowerShell 下，在仓库根目录设置源码路径后可直接运行 Python 入口：

```powershell
conda activate webAgent
$env:PYTHONPATH = "$PWD\src"
$env:BROWSER_USE_SETUP_LOGGING = "false"
python -m browser_use.webretriever --help
```

首次运行需要联网下载 tiktoken 词表。实际浏览任务还需要有效的模型 API 配置和任务文件；本地浏览器使用 `--local-browser`。

本地桌面控制面不取代竞赛入口：`scripts/run.sh`、`result.json`、`capture.json` 与每任务工件目录仍是 Runner 的兼容契约。

## 阶段 2：FastAPI sidecar（开发态）

sidecar 仅监听 `127.0.0.1`，并由桌面 supervisor 通过环境变量提供一次性的 bearer token、launch nonce、状态目录、允许的 WebView origin 和 profile 配置映射。它不会从 HTTP 请求、URL 或 stdout 接收或回显模型密钥。

开发态入口为：

```bash
python -m browser_use.webretriever.desktop.sidecar --state-dir /absolute/local/state-dir
```

启动者必须在进程环境中设置 `WR_SIDECAR_BEARER_TOKEN` 和 `WR_SIDECAR_LAUNCH_NONCE`。可选的 `WR_SIDECAR_PROFILE_CONFIGS_JSON` 是 `{profile_id: 本地配置绝对路径}` 映射；没有 profile 时 sidecar 仍可完成 health/readiness，但 run preflight 会拒绝未知 profile。stdout 仅输出一条 `WR_SIDECAR_LISTENING` 握手，其中包含协议版本、动态端口、PID 与 nonce，绝不包含 bearer token。

API 根路径是 `/api/v1`。每个 REST、SSE 和 health 请求均要求 `Authorization: Bearer <launch-token>`；运行状态和可重放事件保存在 `state-dir/control.sqlite3` 的 WAL journal 中。sidecar 重启会将遗留的活动 run 标记为 `INTERRUPTED`，不会自动重放浏览器动作。

## 阶段 3：Tauri/React 桌面壳（开发态）

桌面壳位于 `desktop/`：Tauri supervisor 为 sidecar 分配一次性 bearer token 和 launch nonce，验证 stdout 握手及 `/health/ready` 后才向 React WebView 提供内存中的连接描述符。它会监听 sidecar 异常退出、撤销旧描述符并有限次重启；关闭窗口时先以 bearer 调用 shutdown、最多等待 65 秒让活动 run 取消并冲刷状态，超时或请求失败才终止整个 sidecar 进程组，避免遗留子进程。

React 仅通过受限的 Tauri command 取得描述符，并以 bearer header 调用本地控制面。它提供运行配置、本机文件/目录选择、preflight、创建 run、SSE 状态恢复、任务状态、错误和工件视图；token 不会写入 localStorage、sessionStorage 或磁盘。默认 capability 仅允许主窗口与文件选择对话框，CSP 生产态只允许连接到 loopback sidecar。

开发时先准备 Python 环境，再启动 Tauri：

```bash
conda activate Browser-Use
cd desktop
npm install
npm run tauri dev
```

首次构建 Linux Tauri 应用还需要 WebKitGTK、GTK、AppIndicator、librsvg 和 D-Bus 开发包。阶段 3 从源码启动 Python sidecar；将 sidecar、Python 运行时和 Chromium 打进发行包属于阶段 4。

## 阶段 4：发布包

发布工具入口是 `python -m browser_use.webretriever.desktop.release`。它只接受构建机的原生目标：Linux 支持 `x86_64-unknown-linux-gnu` 和 `aarch64-unknown-linux-gnu`，Windows 支持 x64 `x86_64-pc-windows-msvc`，macOS 支持 Intel `x86_64-apple-darwin` 和 Apple Silicon `aarch64-apple-darwin`。macOS 代码签名与公证尚未实现。

在受支持 Linux 构建机上，先用受锁定的 `Browser-Use` 环境准备 PyInstaller 和 Playwright browser，再生成一个独立发行资源目录：

```bash
conda activate Browser-Use
export PYTHONPATH="$PWD/src"
python -m playwright install chromium
python -m browser_use.webretriever.desktop.release build \
  --playwright-browsers-dir "$HOME/.cache/ms-playwright" \
  --output-dir dist/webretriever-linux-resources \
  --target x86_64-unknown-linux-gnu \
  --sidecar-build "$(git rev-parse --verify HEAD)" \
  --runner-build "$(git rev-parse --verify HEAD)"
python -m browser_use.webretriever.desktop.release verify \
  --bundle-dir dist/webretriever-linux-resources
python -m browser_use.webretriever.desktop.release smoke \
  --bundle-dir dist/webretriever-linux-resources
```

在 Windows x64 构建机上，使用 PowerShell 执行相同链路：

```powershell
conda activate webAgent
$env:PYTHONPATH = "$PWD\src"
python -m playwright install chromium
$build = git rev-parse --verify HEAD
python -m browser_use.webretriever.desktop.release build `
  --playwright-browsers-dir "$env:LOCALAPPDATA\ms-playwright" `
  --output-dir dist\webretriever-windows-resources `
  --target x86_64-pc-windows-msvc `
  --sidecar-build $build `
  --runner-build $build
python -m browser_use.webretriever.desktop.release verify `
  --bundle-dir dist\webretriever-windows-resources
python -m browser_use.webretriever.desktop.release smoke `
  --bundle-dir dist\webretriever-windows-resources
```

`build` 使用 PyInstaller `onedir`，会复制完整 sidecar 运行时、动态导入资源，以及当前 pinned Playwright 所要求的 Chromium、headless shell 和 FFmpeg revision。生成的 `sidecar/release-manifest.json` 记录每个有效载荷的 SHA-256、目标三元组、sidecar/runner/app build、API major 和 SQLite schema version；`verify` 会拒绝缺失、篡改或版本不兼容的目录。`smoke` 在不继承 `CONDA_PREFIX` 或 `PYTHONPATH` 的环境中完成 bootstrap、认证就绪和 shutdown 检查。

通过 smoke 后，把已经验证的资源树安装到 Tauri 的固定资源槽，再构建安装包：

```bash
python -m browser_use.webretriever.desktop.release install-tauri-resources \
  --bundle-dir dist/webretriever-linux-resources \
  --resources-dir desktop/src-tauri/resources \
  --replace
cd desktop
npm ci
npm run tauri build -- --bundles deb,appimage
```

Windows 上改用以下命令。它会生成 MSI 和 NSIS EXE；未经代码签名时 Windows 会显示发布者未知的 SmartScreen/安装提示。

```powershell
python -m browser_use.webretriever.desktop.release install-tauri-resources `
  --bundle-dir dist\webretriever-windows-resources `
  --resources-dir desktop\src-tauri\resources `
  --replace
Set-Location desktop
npm ci
npm run build:windows
```

macOS 上使用原生构建机执行以下命令。根据构建机架构将目标改为 `x86_64-apple-darwin` 或 `aarch64-apple-darwin`；它会生成对应架构的 DMG。未签名、未公证的测试包会被 Gatekeeper 提示，正式发布前应在 CI 中配置 Apple Developer 签名与公证凭据。

```bash
conda activate webAgent
export PYTHONPATH="$PWD/src"
python -m playwright install chromium
BUILD="$(git rev-parse --verify HEAD)"
python -m browser_use.webretriever.desktop.release build \
  --playwright-browsers-dir "$HOME/Library/Caches/ms-playwright" \
  --output-dir dist/webretriever-macos-resources \
  --target aarch64-apple-darwin \
  --sidecar-build "$BUILD" \
  --runner-build "$BUILD"
python -m browser_use.webretriever.desktop.release verify \
  --bundle-dir dist/webretriever-macos-resources
python -m browser_use.webretriever.desktop.release smoke \
  --bundle-dir dist/webretriever-macos-resources
python -m browser_use.webretriever.desktop.release install-tauri-resources \
  --bundle-dir dist/webretriever-macos-resources \
  --resources-dir desktop/src-tauri/resources \
  --replace
cd desktop
npm ci
npm run build:macos
```

生产态 Tauri 从资源目录中的固定 `sidecar/webretriever-sidecar` 启动（Windows 自动使用 `.exe` 后缀），并且只允许 `tauri://localhost`；开发态仍然使用活动 `Browser-Use` 环境中的 `python -m browser_use.webretriever.desktop.sidecar`。UI 不能传解释器路径或任意 shell 参数。

升级时，release verifier 会核对 API/sidecar major、Tauri app build 和 SQLite schema matrix；若传入 `--state-dir`，会先迁移该目录的 `control.sqlite3`。SQLite 使用 `PRAGMA user_version`，旧的无版本 v1 journal 会原地补全 `last_event_id` 并标记为 schema 1；高于当前版本的数据库会被拒绝，不会降级写入。
