#!/usr/bin/env bash
# 保持 WSL 实例常驻，并确保所有服务处于运行状态
# 由 Windows 登录任务以隐藏窗口方式启动：
#   wsl -d Ubuntu2204 -u root -e /bin/bash /opt/gpuserver/scripts/keepalive.sh
set -uo pipefail

for i in $(seq 1 60); do
  if systemctl is-system-running 2>/dev/null | grep -qE "running|degraded"; then break; fi
  sleep 1
done

systemctl start jupyterhub.service 2>/dev/null || true
systemctl start neo4j.service 2>/dev/null || true
systemctl start whu-llm.service 2>/dev/null || true
systemctl start whu-app.service 2>/dev/null || true

echo "[keepalive] started at $(date -Is), holding instance open"
exec sleep infinity
