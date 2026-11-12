#!/usr/bin/env bash
# 安装 llama.cpp（CPU 版）+ 下载 Qwen2.5-1.5B-Instruct GGUF
set -euo pipefail
mkdir -p /var/log/gpuserver /opt/llama /opt/models
LOG=/var/log/gpuserver/llm-setup.log
exec > >(tee -a "$LOG") 2>&1

LLAMA_TAG=b12242
LLAMA_URL="https://gh-proxy.com/https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_TAG}/llama-${LLAMA_TAG}-bin-ubuntu-x64.tar.gz"
MODEL_FILE=qwen2.5-1.5b-instruct-q4_k_m.gguf
MODEL_URL="https://www.modelscope.cn/api/v1/models/Qwen/Qwen2.5-1.5B-Instruct-GGUF/repo?Revision=master&FilePath=${MODEL_FILE}"

echo "===== llm setup start $(date -Is) ====="

echo "--- llama.cpp binary"
cd /opt/llama
if [ ! -f llama.tar.gz ] || [ ! -s llama.tar.gz ]; then
  curl -fL --retry 5 --retry-delay 3 --max-time 600 -o llama.tar.gz "$LLAMA_URL"
fi
ls -lh llama.tar.gz
tar -xzf llama.tar.gz
find /opt/llama -maxdepth 3 -name "llama-server" -type f

echo "--- GGUF model"
cd /opt/models
if [ ! -f "$MODEL_FILE" ] || [ ! -s "$MODEL_FILE" ]; then
  curl -fL --retry 5 --retry-delay 3 --max-time 3600 -o "$MODEL_FILE" "$MODEL_URL"
fi
ls -lh "$MODEL_FILE"

echo "--- smoke test"
LLAMA_SERVER=$(find /opt/llama -maxdepth 3 -name llama-server -type f | head -1)
"$LLAMA_SERVER" --version 2>&1 | head -5 || true

echo "===== llm setup done $(date -Is) ====="
