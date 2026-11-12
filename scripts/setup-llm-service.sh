#!/usr/bin/env bash
# 注册并启动本地 LLM 服务（llama.cpp + Qwen2.5-1.5B）
set -euo pipefail
LOG=/var/log/gpuserver/llm-service.log
exec > >(tee -a "$LOG") 2>&1

BIN=/opt/llama/llama-b12242/llama-server
MODEL=/opt/models/qwen2.5-1.5b-instruct-q4_k_m.gguf

echo "===== llm service setup $(date -Is) ====="
[ -x "$BIN" ] || { echo "missing $BIN"; exit 1; }
[ -s "$MODEL" ] || { echo "missing $MODEL"; exit 1; }

cat > /etc/systemd/system/whu-llm.service <<EOF
[Unit]
Description=WHU GraphRAG local LLM (llama.cpp + Qwen2.5-1.5B)
After=network.target

[Service]
Type=simple
ExecStart=$BIN -m $MODEL --host 127.0.0.1 --port 8081 -c 4096 -t 3 -b 256 --no-warmup
Restart=always
RestartSec=5
Nice=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable whu-llm >/dev/null 2>&1 || true
systemctl restart whu-llm

echo "--- waiting for server"
for i in $(seq 1 60); do
  if curl -s --max-time 3 http://127.0.0.1:8081/health >/dev/null 2>&1; then echo "healthy after ${i}s"; break; fi
  sleep 2
done
systemctl is-active whu-llm || true

echo "--- smoke completion"
curl -s --max-time 180 http://127.0.0.1:8081/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen","messages":[{"role":"user","content":"用一句话介绍武汉大学。"}],"max_tokens":64,"temperature":0.2}' \
  | head -c 600
echo
echo "===== done $(date -Is) ====="
