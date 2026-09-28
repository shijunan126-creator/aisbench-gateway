#!/usr/bin/env bash
# 各脚本共用的函数。
#
# 这里**不允许调用宿主机 python** —— 整个交付包的目标就是客户机器只需要 Docker。
# config.ini 的值一律用 awk 解析。

# 包目录（本文件所在目录）
GW_PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GW_CONFIG="$GW_PKG_DIR/config.ini"
GW_DATA_HOST="$GW_PKG_DIR/data"
GW_MOUNT_POINT="/gateway"

# ---------------------------------------------------------------- 输出
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m[%s]\033[0m %s\n' "$1" "$2"; }

# ---------------------------------------------------------------- 首次运行自举
# 确保 config.ini 存在。
#
# 仓库里 config.ini 是 gitignore 的（各人宿主机路径不同，不该提交），
# 它由 config.ini.example 复制而来 —— 后者由 gw/settings.py 的 TEMPLATE
# 生成，保持单一真源，别手改。
#
# **不做这一步，全新克隆下来第一次 ./start.sh 会直接失败**：这些脚本是从
# config.ini 用 awk 读镜像名的（不走 Python），读不到就把空字符串传给
# docker run，报 `docker: invalid reference format`。实测踩到过。
ensure_config() {
  [ -f "$GW_CONFIG" ] && return 0
  if [ -f "$GW_PKG_DIR/config.ini.example" ]; then
    cp "$GW_PKG_DIR/config.ini.example" "$GW_CONFIG"
    ok "已生成 config.ini（首次运行，来自 config.ini.example）"
  else
    bad "缺少 config.ini，也没有 config.ini.example"
    echo "     交付包可能不完整；从仓库克隆的话请确认 config.ini.example 在。" >&2
    return 1
  fi
}

# ---------------------------------------------------------------- 配置读取
#
# 注意方括号的处理：节名是 `[aisbench]`，**不能**直接把 "[" section "]"
# 拼进 awk 的正则里 —— 方括号在正则里是字符类，`[aisbench]` 会变成
# "匹配 a/i/s/b/e/n/c/h 中任意一个字符"，导致永远匹配不上。
# 必须写成 \[ 和 \] 转义。

# ini_get <section> <key> [file]
ini_get() {
  local section="$1" key="$2" file="${3:-$GW_CONFIG}"
  [ -f "$file" ] || return 0
  awk -v want_sec="$section" -v want_key="$key" '
    function is_section(line) {
      return line ~ ("^[[:space:]]*\\[" want_sec "\\][[:space:]]*$")
    }
    /^[[:space:]]*[#;]/ { next }
    /^[[:space:]]*\[/ { in_sec = is_section($0); next }
    in_sec && $0 ~ ("^[[:space:]]*" want_key "[[:space:]]*=") {
      sub(/^[^=]*=/, ""); gsub(/^[[:space:]]+|[[:space:]]+$/, ""); print; exit
    }
  ' "$file"
}

# ini_set <section> <key> <value> [file]  —— 就地改，没有就追加
ini_set() {
  local section="$1" key="$2" value="$3" file="${4:-$GW_CONFIG}"
  local tmp; tmp="$(mktemp)"
  awk -v sec="$section" -v k="$key" -v v="$value" '
    function is_section(line) {
      return line ~ ("^[[:space:]]*\\[" sec "\\][[:space:]]*$")
    }
    BEGIN { done=0; in_sec=0 }
    /^[[:space:]]*\[/ {
      if (in_sec && !done) { print k " = " v; done=1 }
      in_sec = is_section($0)
      print; next
    }
    in_sec && $0 ~ ("^[[:space:]]*" k "[[:space:]]*=") {
      if (!done) { print k " = " v; done=1 }
      next
    }
    { print }
    END {
      if (!done) {
        if (!in_sec) { print ""; print "[" sec "]" }
        print k " = " v
      }
    }
  ' "$file" > "$tmp" && mv "$tmp" "$file"
}

gw_container()  { ini_get aisbench container; }
gw_image()      { ini_get aisbench image; }
gw_port()       { local p; p="$(ini_get server port)"; echo "${p:-8080}"; }
gw_host()       { local h; h="$(ini_get server host)"; echo "${h:-0.0.0.0}"; }

# ---------------------------------------------------------------- docker
require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    bad "找不到 docker 命令"
    echo "     请先安装 Docker：https://docs.docker.com/engine/install/"
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    bad "无法连接 Docker 服务"
    echo "     排查："
    echo "       1) 启动服务： sudo systemctl start docker"
    echo "       2) 权限不足时把自己加入 docker 组："
    echo "            sudo usermod -aG docker \$USER   # 之后需要重新登录"
    echo "          或者用 sudo 重新执行本脚本。"
    return 1
  fi
  return 0
}

container_exists()  { docker inspect "$(gw_container)" >/dev/null 2>&1; }
container_running() { [ "$(docker inspect -f '{{.State.Running}}' "$(gw_container)" 2>/dev/null)" = "true" ]; }

# 生成 tokenizer 目录的挂载参数（写成一行一个 -v 参数）
tokenizer_mount_args() {
  local dirs raw
  raw="$(ini_get aisbench tokenizer_dirs)"
  [ -z "$raw" ] && return 0
  IFS=',' read -ra dirs <<< "$raw"
  for d in "${dirs[@]}"; do
    d="$(echo "$d" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    [ -z "$d" ] && continue
    if [ -d "$d" ]; then
      printf -- '-v\n%s:%s:ro\n' "$d" "$d"
    fi
  done
}

# 启动（或创建）网关容器
start_container() {
  local name image port image_now
  name="$(gw_container)"; image="$(gw_image)"; port="$(gw_port)"

  local args=()
  while IFS= read -r line; do args+=("$line"); done < <(tokenizer_mount_args)

  docker run -d \
    --name "$name" \
    --network host \
    --restart unless-stopped \
    -v "$GW_PKG_DIR:$GW_MOUNT_POINT:ro" \
    -v "$GW_DATA_HOST:/work" \
    "${args[@]}" \
    -w "$GW_MOUNT_POINT" \
    -e AIS_BENCH_DATASETS_CACHE=/work/datasets \
    -e PYTHONUNBUFFERED=1 \
    "$image" \
    python3 -m gw.main >/dev/null
}

# 去掉尾部斜杠。
#
# **Docker 会规范化挂载路径**：`-v /a/b/:/a/b/:ro` 之后 `docker inspect` 报的是
# `/a/b`（尾斜杠没了）。所以两边必须先规范化再比 —— 否则 config.ini 里
# 路径只要带个尾斜杠，比对就永远不相等，每次 `./stop.sh && ./start.sh`
# 都会误判成「挂载变了」去 `docker rm -f` 重建容器。
# 实测复现过：健康时被 start.sh 的提前返回掩盖，一旦服务不健康就会重建，
# 连带把正在跑的压测 SIGKILL 掉、docker logs 历史也没了。
_norm_path() {
  local p="$1"
  while [ -n "$p" ] && [ "${p%/}" != "$p" ]; do p="${p%/}"; done
  printf '%s' "${p:-/}"
}

# 配置里期望的 tokenizer 挂载源路径。
# tokenizer_mount_args 输出 `-v\n<src>:<dst>:ro`，而 src 与 dst 相同，
# 所以取冒号前那段当源路径即可（路径里含冒号的情况极罕见，两边同样处理）。
desired_tokenizer_mounts() {
  tokenizer_mount_args | grep -v '^-v$' | sed 's/:ro$//' \
    | while IFS= read -r d; do _norm_path "${d%%:*}"; echo; done \
    | sort
}

# 容器**当前**的 tokenizer 挂载源路径（排除固定的代码目录和数据目录）
container_tokenizer_mounts() {
  docker inspect -f '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' "$(gw_container)" 2>/dev/null \
    | grep -v '^$' \
    | grep -vx "$(_norm_path "$GW_PKG_DIR")" \
    | grep -vx "$(_norm_path "$GW_DATA_HOST")" \
    | while IFS= read -r d; do _norm_path "$d"; echo; done \
    | sort
}

# 容器在跑就直接返回；停了就 start；不存在就重建。
#
# 两种「配置改了」的情况都必须重建容器 —— docker start 只会沿用创建时的参数，
# 改了配置却不重建，新配置是**不会生效**的：
#   1. 镜像 tag 变了
#   2. tokenizer_dirs 变了（挂载是在 docker run 时定下的）
# 第 2 条尤其容易踩：文档让用户「改完 config.ini 后 stop + start」，
# 而只 stop + start 不重建的话，容器里根本看不到新配的 tokenizer 目录，
# 表现是提交任务时报「tokenizer 路径不存在」，但那个路径在宿主机上明明存在。
ensure_container() {
  local name image running_image
  name="$(gw_container)"; image="$(gw_image)"

  if container_exists; then
    running_image="$(docker inspect -f '{{.Config.Image}}' "$name" 2>/dev/null || echo "")"
    if [ -n "$image" ] && [ "$running_image" != "$image" ]; then
      warn "容器用的镜像（$running_image）与配置（$image）不一致，重建容器"
      docker rm -f "$name" >/dev/null 2>&1 || true
      start_container
      return $?
    fi
    if [ "$(container_tokenizer_mounts)" != "$(desired_tokenizer_mounts)" ]; then
      warn "tokenizer 挂载与 config.ini 不一致，重建容器使其生效"
      docker rm -f "$name" >/dev/null 2>&1 || true
      start_container
      return $?
    fi
    if container_running; then
      return 0
    fi
    docker start "$name" >/dev/null
    return $?
  fi
  start_container
}

# ---------------------------------------------------------------- 健康检查
# 优先用宿主机的 curl/wget；都没有就用容器内的 python（容器里一定有）
http_ok() {
  local port; port="$(gw_port)"
  local url="http://127.0.0.1:${port}/api/health"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 3 "$url" >/dev/null 2>&1 && return 0 || return 1
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -q -T 3 -O /dev/null "$url" 2>/dev/null && return 0 || return 1
  fi
  docker exec "$(gw_container)" python3 -c "
import urllib.request
urllib.request.urlopen('$url', timeout=3).read()
" >/dev/null 2>&1
}

wait_healthy() {
  local tries="${1:-40}" i
  for i in $(seq 1 "$tries"); do
    if http_ok; then return 0; fi
    sleep 0.5
  done
  return 1
}

# ---------------------------------------------------------------- 展示
print_access_url() {
  local port ip; port="$(gw_port)"
  # `|| true`：busybox/精简系统的 hostname 不支持 -I，赋值失败会被 set -e 终止，
  # 而那时还没打印访问地址，用户什么都看不到
  ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  echo "  本机访问  ：http://127.0.0.1:${port}"
  [ -n "${ip:-}" ] && echo "  局域网访问：http://${ip}:${port}"
}

# 架构不匹配的提示。
#
# 以前这里写死「x86 就是靠 QEMU 模拟 arm64 镜像，会频繁死锁」—— **那个前提是错的**：
# 镜像本身是多平台的，在 x86 上跑的是原生 amd64（实测容器内 uname -m 得 x86_64、
# ELF 头 EM_X86_64）。照那段话会让 x86 用户以为自己的机器不行。
#
# 现在改成查**实际装着的镜像**的架构再和本机比，只在真的不一致时才说话 ——
# 那才是会真正导致容器起不来的情况。
print_arch_warning() {
  local host img imgarch
  host="$(uname -m)"
  case "$host" in
    x86_64|amd64)  host=amd64 ;;
    aarch64|arm64) host=arm64 ;;
  esac
  img="$(gw_image)"
  imgarch="$(docker image inspect "$img" --format '{{.Architecture}}' 2>/dev/null || true)"
  [ -z "$imgarch" ] && return 0
  if [ "$imgarch" != "$host" ]; then
    warn "镜像架构（$imgarch）与本机（$host）不一致，容器起不来"
    echo "     这是交付包和机器不匹配，需要换包或换机器 —— 不是配置问题。"
    echo "     在能上网的机器上取对应架构的镜像："
    echo "       docker pull --platform linux/$host $img"
  fi
  return 0
}
