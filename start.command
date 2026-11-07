#!/bin/bash
# macOS 双击启动：Finder 里双击本文件即可。
# 保持弹出的终端窗口开着，服务就会一直运行；关闭窗口 = 停止服务。
cd "$(dirname "$0")" || exit 1
PORT="${1:-8300}"

echo "=========================================="
echo " 青理课表助手 正在启动…"
echo " 浏览器地址： http://127.0.0.1:${PORT}"
echo " 保持这个窗口开着，关闭窗口即停止服务"
echo "=========================================="

if [ ! -d .venv ]; then
  echo "首次运行，正在创建虚拟环境并安装依赖…"
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi

# 等服务起来后自动打开浏览器
( sleep 2 && open "http://127.0.0.1:${PORT}" ) >/dev/null 2>&1 &

exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
