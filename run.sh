#!/usr/bin/env bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

echo "=========================================================="
echo "   启动 Z.ai 账号批量注册工作台 (WebUI) ... "
echo "=========================================================="

if [ ! -d ".venv" ]; then
    echo "创建 Python 虚拟环境..."
    python3 -m venv .venv
    .venv/bin/pip install fastapi uvicorn websockets playwright jinja2 python-multipart requests pysocks
    .venv/bin/playwright install chromium
fi

echo "服务访问地址: http://127.0.0.1:8765"
echo "按 Ctrl+C 可停止服务"

exec .venv/bin/python3 -m uvicorn app:app --host 127.0.0.1 --port 8765 --reload
