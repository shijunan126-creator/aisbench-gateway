#!/usr/bin/env bash
# 前台运行网关，实时看日志。Ctrl-C 即停止。
#
#   ./run.sh
#
# 与 ./start.sh 的区别：这个是前台、退出即停，适合排障。
# 日常使用请用 ./start.sh（后台常驻、开机自启）。
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck source=lib.sh
. ./lib.sh
ensure_config

if ! require_docker; then exit 1; fi

NAME="$(gw_container)"
IMAGE="$(gw_image)"

if container_exists; then
  echo "先移除已有容器 ${NAME}（数据目录不受影响）…"
  docker rm -f "$NAME" >/dev/null 2>&1 || true
fi

mkdir -p "$GW_DATA_HOST/models" "$GW_DATA_HOST/configs" "$GW_DATA_HOST/outputs"

TOK_ARGS=()
while IFS= read -r line; do TOK_ARGS+=("$line"); done < <(tokenizer_mount_args)

echo "前台启动网关，Ctrl-C 停止"
print_access_url
echo

exec docker run --rm -it \
  --name "$NAME" \
  --network host \
  -v "$GW_PKG_DIR:$GW_MOUNT_POINT:ro" \
  -v "$GW_DATA_HOST:/work" \
  "${TOK_ARGS[@]}" \
  -w "$GW_MOUNT_POINT" \
  -e AIS_BENCH_DATASETS_CACHE=/work/datasets \
  -e PYTHONUNBUFFERED=1 \
  "$IMAGE" \
  python3 -m gw.main
