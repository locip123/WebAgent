# 启动服务
cli-proxy-api -config /root/config.yaml
# 登录账号
cli-proxy-api -codex-device-login
 # 查看两个服务是否在运行
  tmux list-sessions

  # 查看 Keeper 状态与实时日志
  tmux attach -t cpa-usage-keeper
  # 按 Ctrl+B，再按 D 可退出日志界面但不停止服务

  # 停止 Keeper
  tmux kill-session -t cpa-usage-keeper

  # 启动 Keeper
  tmux new-session -d -s cpa-usage-keeper \
    'cd /root/cpa-usage-keeper && exec ./cpa-usage-keeper'

  # 重启 Keeper
  tmux kill-session -t cpa-usage-keeper
  tmux new-session -d -s cpa-usage-keeper \
    'cd /root/cpa-usage-keeper && exec ./cpa-usage-keeper'

  CLIProxyAPI 当前在 CliProxyApi tmux 会话中：

  # 查看 CLIProxyAPI 日志
  tmux attach -t CliProxyApi

  # 若它未运行，才启动（运行时不要重复执行）
  tmux new-session -d -s CliProxyApi \
    'exec cli-proxy-api -config /root/config.yaml'

  打开面板：http://127.0.0.1:8080。

  # 密码
  Keeper-Login-6zr8Qp2Lm5Vx9Nd



