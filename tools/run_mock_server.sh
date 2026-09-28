#!/usr/bin/env bash
# 启动内置的假模型服务，用于在没有真实模型服务时演练网关流程。
#
#   ./tools/run_mock_server.sh
#   ./tools/run_mock_server.sh --port 9000 --ttft-ms 100 --tpot-ms 8
#   ./tools/run_mock_server.sh --stop
#
# 它跑在**网关容器内**，所以宿主机同样不需要 python。
# 因为容器是 --network host，宿主机和容器都能用 127.0.0.1:8000 访问到它。
#
# 它返回的是随机词，**精度得分没有意义**，只用来验证流程。
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=lib.sh
. ./lib.sh

NAME="$(gw_container)"

if ! command -v docker >/dev/null 2>&1; then
  bad "找不到 docker 命令"; exit 1
fi
if ! container_running; then
  bad "网关容器没在运行，先执行 ./start.sh"
  exit 1
fi

if [ "${1:-}" = "--stop" ]; then
  docker exec "$NAME" pkill -f mock_openai_server 2>/dev/null || true
  echo "假模型服务已停止"
  exit 0
fi

if docker exec "$NAME" pgrep -f mock_openai_server >/dev/null 2>&1; then
  echo "假模型服务已经在跑"
  exit 0
fi

# -d 让它脱离终端在容器内后台运行
docker exec -d "$NAME" python3 "$GW_MOUNT_POINT/tools/mock_openai_server.py" "$@"

sleep 2
if docker exec "$NAME" pgrep -f mock_openai_server >/dev/null 2>&1; then
  ok "假模型服务已启动（容器内，端口 8000）"
  echo
  echo "  页面上这样填："
  echo "    Base URL : http://127.0.0.1:8000"
  echo "    模型名   : mock-model"
  echo "    数据集   : 随机数据集（内置）"
  echo
  echo "  停止：./tools/run_mock_server.sh --stop"
else
  bad "启动失败"
  exit 1
fi
