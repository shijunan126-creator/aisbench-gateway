#!/usr/bin/env bash
# 停止网关容器。
#
#   ./stop.sh           停止（保留容器，下次启动更快）
#   ./stop.sh --purge   停止并删除容器（连同配置里的镜像一起清掉？不会，只删容器）
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck source=lib.sh
. ./lib.sh

if ! command -v docker >/dev/null 2>&1; then
  bad "找不到 docker 命令"; exit 1
fi

NAME="$(gw_container)"

if ! container_exists; then
  echo "容器不存在，无需停止"
  exit 0
fi

if container_running; then
  # 网关装了 SIGTERM 处理器，这里是优雅停止，秒级返回
  docker stop -t 20 "$NAME" >/dev/null 2>&1 || true
  echo "网关已停止"
else
  echo "网关本来就没在运行"
fi

if [ "${1:-}" = "--purge" ]; then
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  echo "容器已删除（数据目录未动：$GW_DATA_HOST）"
fi
