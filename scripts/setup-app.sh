#!/usr/bin/env bash
# 注册 GraphRAG Web 应用服务
set -euo pipefail
LOG=/var/log/gpuserver/whu-app-setup.log
exec > >(tee -a "$LOG") 2>&1

echo "===== whu-app service setup $(date -Is) ====="

mkdir -p /opt/whu/app /opt/whu/data
cp /mnt/c/Users/wanyo/Documents/deepseek-harness/default-workspace/whu/app/app.py /opt/whu/app/
cp /mnt/c/Users/wanyo/Documents/deepseek-harness/default-workspace/whu/app/index.html /opt/whu/app/

cat > /etc/systemd/system/whu-app.service <<'EOF'
[Unit]
Description=WHU Admissions GraphRAG web app
After=network.target whu-llm.service
Wants=whu-llm.service

[Service]
Type=simple
WorkingDirectory=/opt/whu/app
Environment=WHU_DATA=/opt/whu/data
Environment=WHU_LLM=http://127.0.0.1:8082/v1/chat/completions
Environment=WHU_PORT=8090
ExecStart=/opt/dl/venv/bin/python /opt/whu/app/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable whu-app >/dev/null 2>&1 || true
systemctl restart whu-app
sleep 8
systemctl is-active whu-app || true
curl -s -o /dev/null -w "app HTTP %{http_code}\n" --max-time 10 http://127.0.0.1:8090/ || true
curl -s --max-time 10 http://127.0.0.1:8090/api/stats || true
echo
echo "===== done ====="
