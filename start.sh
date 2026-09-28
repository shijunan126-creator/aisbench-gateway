#!/usr/bin/env bash
# 启动网关容器（后台），等到服务真正可用才返回。
#
#   ./start.sh
#
# 容器不存在就创建，停着就启动，镜像 tag 变了就重建。
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck source=lib.sh
. ./lib.sh

if ! require_docker; then exit 1; fi

NAME="$(gw_container)"
if container_running && http_ok; then
  echo "网关已经在运行"
  print_access_url
  exit 0
fi

mkdir -p "$GW_DATA_HOST/models"

if ! ensure_container; then
  bad "容器启动失败"
  echo "     看日志：docker logs $NAME"
  exit 1
fi

if wait_healthy 60; then
  echo "网关已启动"
  print_access_url
  echo "  日志：docker logs -f $NAME"
  echo "  停止：./stop.sh"
else
  bad "网关启动超时"
  echo "     容器日志："
  docker logs --tail 30 "$NAME" 2>&1 | sed 's/^/       /'
  exit 1
fi
