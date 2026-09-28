#!/usr/bin/env bash
# 查看运行状态与诊断信息。
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck source=lib.sh
. ./lib.sh

NAME="$(gw_container)"
IMAGE="$(gw_image)"
PORT="$(gw_port)"

echo "===== AISBench 网关状态 ====="

# ---- 容器 ----
if container_exists; then
  STATE="$(docker inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null || echo unknown)"
  echo "网关容器    : ${NAME} (${STATE})"
  USED_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$NAME" 2>/dev/null || echo '')"
  echo "容器用的镜像: ${USED_IMAGE##*/}"
  if [ -n "$IMAGE" ] && [ "$USED_IMAGE" != "$IMAGE" ]; then
    warn "与 config.ini 里配置的镜像不一致（${IMAGE##*/}），下次 ./start.sh 会重建容器"
  fi
  STARTED="$(docker inspect -f '{{.State.StartedAt}}' "$NAME" 2>/dev/null || echo '')"
  [ -n "$STARTED" ] && echo "启动时间    : ${STARTED%%.*}"
else
  echo "网关容器    : 不存在（执行 ./start.sh 创建）"
fi

# ---- 服务 ----
if container_running && http_ok; then
  echo "服务健康    : 正常"
else
  echo "服务健康    : 不可用"
fi
# 变量名要和下面判断的一致（以前写成 IP= 却判断 ${ip:-}，条件恒假，
# 局域网地址一次都没打印出来过）；`|| true` 是防 `hostname -I` 在
# busybox/精简系统上不支持时，被 set -e 直接终止脚本。
IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
echo "访问地址    : http://127.0.0.1:${PORT}"
[ -n "${IP:-}" ] && echo "              http://${IP}:${PORT}"

# ---- 镜像 ----
if [ -n "$IMAGE" ] && docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "aisbench镜像: 已导入"
else
  echo "aisbench镜像: 未导入 —— 请先运行 ./install.sh"
fi

# ---- 数据 ----
echo "包目录      : $GW_PKG_DIR"
echo "数据目录    : $GW_DATA_HOST"
DS="$GW_DATA_HOST/datasets/ais_bench/datasets"
if [ -d "$DS" ]; then
  N="$(find "$DS" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)"
  echo "已就绪数据集: ${N} 个"
fi
if [ -d "$GW_DATA_HOST/models" ]; then
  M="$(find "$GW_DATA_HOST/models" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)"
  echo "本地tokenizer: ${M} 个（放在 $GW_DATA_HOST/models/ 下）"
fi

# ---- 运行环境 ----
echo
echo "===== 运行环境 ====="
echo "架构        : $(uname -m)"
print_arch_warning
if command -v docker >/dev/null 2>&1; then
  echo "Docker      : 可用（$(docker --version 2>/dev/null | head -c 50)）"
else
  echo "Docker      : 不可用"
fi

# ---- 最近日志 ----
if container_exists; then
  echo
  echo "===== 最近日志 ====="
  docker logs --tail 10 "$NAME" 2>&1 | sed 's/^/  /'
fi
