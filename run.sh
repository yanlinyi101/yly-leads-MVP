#!/usr/bin/env bash
# 一键启动开发服务器
set -e

if [ ! -f .env ]; then
  echo "请先复制 .env.example 为 .env 并填入 DEEPSEEK_API_KEY"
  exit 1
fi

# shellcheck disable=SC1091
source .env

uvicorn backend.main:app --reload --host "${APP_HOST:-127.0.0.1}" --port "${APP_PORT:-8000}"

