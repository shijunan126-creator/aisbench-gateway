#!/usr/bin/env bash
# ============================================================
#  AISBench 网关 —— 一键开局
#
#  做五件事：检查环境 → 架构校验 → 配置模型目录 → 导入镜像 → 启动
#  可以重复执行（幂等），中途出错修好后直接重跑即可。
#
#  客户机器**只需要 Docker** ——不需要 python3，不需要联网，不需要 pip。
#
#    ./install.sh                        交互安装（会强制要求模型/tokenizer 目录）
#    ./install.sh --tokenizer /path/to   非交互指定模型/tokenizer 目录
#    ./install.sh --yes --tokenizer /p   全部用默认值（--yes 必须配 --tokenizer）
#    ./install.sh --port 9000            指定端口
# ============================================================
set -uo pipefail

cd "$(dirname "$0")"
# shellcheck source=lib.sh
. ./lib.sh
ensure_config

ASSUME_YES=0
PORT_OVERRIDE=""
TOKENIZER_INPUT=""

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes)     ASSUME_YES=1 ;;
    --port)       PORT_OVERRIDE="${2:-}"; shift ;;
    --tokenizer)  TOKENIZER_INPUT="${2:-}"; shift ;;
    -h|--help)    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
  esac
  shift
done

is_tty() { [ -t 0 ] && [ -t 1 ]; }
die() { bad "$*"; echo; echo "安装中止。修好上面的问题后重新执行 ./install.sh 即可。"; exit 1; }

ask() {  # ask <提示> <默认值> → REPLY
  local prompt="$1" default="${2:-}"
  if [ "$ASSUME_YES" = "1" ] || ! is_tty; then REPLY="$default"; return 0; fi
  if [ -n "$default" ]; then printf '  %s [%s]: ' "$prompt" "$default"
  else printf '  %s: ' "$prompt"; fi
  read -r REPLY || REPLY="$default"
  [ -z "$REPLY" ] && REPLY="$default"
  return 0
}

# 目录里是否像是个 tokenizer/模型目录。列表没有穷举（自定义 tokenizer
# 可能只有 config.json + 权重），只做提醒用，不是硬校验。
looks_like_tokenizer() {
  local d="$1" f
  for f in tokenizer.json tokenizer_config.json vocab.json vocab.txt \
           merges.txt spiece.model sentencepiece.bpe.model tokenizer.model; do
    [ -f "$d/$f" ] && return 0
  done
  return 1
}

# ---------- 模型/tokenizer 目录（强制配置） ----------
#
# 随机数据集 / sharegpt / GSM8K 前缀数据集没有它跑不了，而每台机器的
# 模型路径都不一样，所以安装时**必须**给一个，不能留空跳过：
#   - 交互模式：循环要求输入，目录不存在就重新输入
#   - --yes / 非交互：必须配 --tokenizer 参数，否则直接报错
#   - config.ini 里已经配过：回车即可保留（重复执行安装不用重填）
require_tokenizer() {
  local existing input
  existing="$(ini_get aisbench tokenizer_dirs)"

  if [ -n "$TOKENIZER_INPUT" ]; then
    # --tokenizer 参数：直接采用，目录不对就中止
    if [ ! -d "$TOKENIZER_INPUT" ]; then
      die "--tokenizer 目录不存在：$TOKENIZER_INPUT"
    fi
    REPLY="$(cd "$TOKENIZER_INPUT" && pwd)"
  elif [ -n "$existing" ]; then
    ask "模型/tokenizer 目录（回车保留现有：$existing）" "$existing"
  else
    printf '  模型/tokenizer 目录（必填）：随机数据集、GSM8K 前缀数据集、sharegpt\n'
    printf '  都需要它做 token 计数。填模型所在目录，或装了多个模型的父目录，如\n'
    printf '  /data/models （网关会扫描其中的模型文件夹，页面上按名字选择）\n'
    while : ; do
      if ! is_tty || [ "$ASSUME_YES" = "1" ]; then
        die "未提供模型/tokenizer 目录。非交互安装必须加参数：./install.sh --tokenizer /path/to/model"
      fi
      printf '  目录路径: '
      read -r input || input=""
      [ -z "$input" ] && { bad "不能为空，请输入模型/tokenizer 目录"; continue; }
      if [ ! -d "$input" ]; then
        bad "目录不存在：$input，请重新输入"
        continue
      fi
      REPLY="$input"
      break
    done
  fi

  TOK_ABS="$(cd "$REPLY" && pwd)"
  if looks_like_tokenizer "$TOK_ABS"; then
    ok "已配置模型/tokenizer 目录：$TOK_ABS"
  else
    # 不是模型目录本身？按装了多个模型的父目录处理，扫一层子目录
    N_FOUND=0
    for sub in "$TOK_ABS"/*/; do
      [ -d "$sub" ] || continue
      if looks_like_tokenizer "${sub%/}"; then N_FOUND=$((N_FOUND+1)); fi
    done
    if [ "$N_FOUND" -gt 0 ]; then
      ok "已配置模型目录：$TOK_ABS（识别到其中 ${N_FOUND} 个模型文件夹，页面上按名字选择）"
    else
      warn "目录及其一级子目录里都没找到 tokenizer.json / vocab.json 等特征文件"
      echo "     如果这里放的是完整模型目录（含权重），忽略此提醒即可。"
    fi
  fi
  ini_set aisbench tokenizer_dirs "$TOK_ABS"
  echo "     该目录按原路径只读挂进容器；改动后需 ./stop.sh && ./start.sh 重建容器。"
}

echo "============================================"
echo "  AISBench 网关 安装程序"
echo "  目录：$GW_PKG_DIR"
echo "============================================"

# ---------- 1. Docker ----------
step 1/6 "检查 Docker"
require_docker || exit 1
ok "Docker 可用（$(docker --version 2>/dev/null | head -c 50)）"

# ---------- 2. 架构 ----------
step 2/6 "检查机器架构"
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
  x86_64|amd64)   HOST_ARCH=amd64 ;;
  aarch64|arm64)  HOST_ARCH=arm64 ;;
esac

# 包里装的镜像是**单平台**的（见 make-release.sh::to_single_platform），
# make-release.sh 会把它写进 manifest.json。所以这里拿包的架构和本机对一下 ——
# 不一致的话 docker run 必然报平台不匹配，早报比装到一半再报好。
#
# 以前这里写的是"镜像只有 arm64、x86 要靠 QEMU 模拟、会频繁死锁"。那个前提
# **是错的**：镜像其实是多平台的，在 x86 上就是原生 amd64 在跑（实测容器内
# uname -m 得 x86_64、ELF 头是 EM_X86_64）。照那段话，x86 用户会被劝退，
# 而按回车（默认 N）还会直接取消安装。
# 用 sed 而不是 python3 解析：**客户机上没有 python3**，只有 Docker，
# 这是整个交付方案的前提（见 README「不需要 python3」）。
PKG_ARCH=""
if [ -f "$GW_PKG_DIR/manifest.json" ]; then
  PKG_ARCH="$(sed -n 's/.*"arch"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
    "$GW_PKG_DIR/manifest.json" 2>/dev/null | head -1 || true)"
fi

if [ -n "$PKG_ARCH" ] && [ "$PKG_ARCH" != "$HOST_ARCH" ]; then
  warn "这个交付包是 **$PKG_ARCH** 架构的，而本机是 $HOST_ARCH"
  echo
  echo "     包里的镜像已被裁成单平台，在本机上 docker run 会直接报"
  echo "     「image with reference ... does not match the specified platform」，"
  echo "     容器根本起不来。请换成 $PKG_ARCH 的机器，或向提供方索取"
  echo "     $HOST_ARCH 版本的交付包。"
  echo
  die "架构不匹配，未做任何改动"
elif [ -n "$PKG_ARCH" ]; then
  ok "架构 $HOST_ARCH，与交付包内的镜像一致（原生运行，无模拟）"
else
  warn "包里没有 manifest.json，无法确认镜像架构，继续安装"
fi

# ---------- 3. 模型/tokenizer 目录（强制） ----------
step 3/6 "配置模型/tokenizer 目录（必填）"
require_tokenizer

# ---------- 4. 镜像 ----------
step 4/6 "导入 aisbench 镜像"

IMAGE="$(gw_image)"
[ -z "$IMAGE" ] && die "config.ini 里没读到 aisbench.image，请检查配置文件"

IMG_TAR=""
for cand in images/aisbench_benchmark.tar images/*.tar; do
  [ -f "$cand" ] && { IMG_TAR="$cand"; break; }
done

if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  ok "镜像已存在，跳过导入：${IMAGE##*/}"
else
  if [ -z "$IMG_TAR" ]; then
    bad "找不到镜像文件（images/*.tar）"
    echo "     交付包可能不完整，请重新解压完整的压缩包。"
    echo "     或在能上网的机器上执行：docker pull $IMAGE"
    exit 1
  fi

  echo "  正在导入 $IMG_TAR（$(du -h "$IMG_TAR" | cut -f1)），需要几分钟，请稍候…"
  LOAD_LOG="$(mktemp)"
  if ! docker load -i "$IMG_TAR" >"$LOAD_LOG" 2>&1; then
    sed 's/^/    /' "$LOAD_LOG"; rm -f "$LOAD_LOG"
    die "镜像导入失败，请确认磁盘空间充足（至少需要 10GB 可用）"
  fi
  sed 's/^/    /' "$LOAD_LOG"
  # docker load 即使打印了 Error unpacking 也可能返回 0，所以要查日志内容
  if grep -qE "Error unpacking|failed to extract layer|content digest .* not found" "$LOAD_LOG"; then
    rm -f "$LOAD_LOG"
    die "镜像导入不完整（层数据缺失）。交付包里的 images/*.tar 可能损坏，请重新获取。"
  fi
  rm -f "$LOAD_LOG"
  ok "镜像导入完成"
fi

# 真跑一次确认可用 —— 只看 docker load 的返回码不够可靠
echo "  验证镜像可用性 …"
if docker run --rm --entrypoint ais_bench "$IMAGE" --help >/dev/null 2>&1; then
  ok "镜像可用"
else
  warn "镜像存在但 ais_bench 跑不起来"
  echo "     手动试一下看具体报错："
  echo "       docker run --rm --entrypoint ais_bench $IMAGE --help"
  # 最常见的两种原因，直接把话说清楚，别让用户猜
  echo "     常见原因：① 镜像平台与本机不符（上面第 2 步会报出来）；"
  echo "               ② 镜像层数据缺失（交付包损坏，重新解压一次）"
fi

# ---------- 5. 数据与配置 ----------
step 5/6 "准备数据与配置"

# generated/ 放合成数据集（GSM8K 前缀数据集），网关启动时也会建，
# 这里先建出来是为了让客户一眼能看到数据目录里有哪几块
mkdir -p "$GW_DATA_HOST/models" "$GW_DATA_HOST/configs" "$GW_DATA_HOST/outputs" \
         "$GW_DATA_HOST/generated"

# 预置数据集：首次安装铺开，已存在不覆盖
SEED="$GW_PKG_DIR/seed/ais_bench/datasets"
DEST="$GW_DATA_HOST/datasets/ais_bench/datasets"
if [ -d "$SEED" ]; then
  mkdir -p "$DEST"
  COPIED=0
  for d in "$SEED"/*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    [ -e "$DEST/$name" ] && continue
    cp -r "$d" "$DEST/$name" && COPIED=$((COPIED+1))
  done
  if [ "$COPIED" -gt 0 ]; then ok "铺开 $COPIED 个预置数据集"
  else ok "预置数据集已就绪（未重复覆盖）"; fi
else
  warn "交付包里没有预置数据集（seed/ 缺失），可在页面上按需下载"
fi

# tokenizer 已在第 3 步强制配置（require_tokenizer）

if [ -n "$PORT_OVERRIDE" ]; then
  ini_set server port "$PORT_OVERRIDE"
fi

ok "配置文件：$GW_CONFIG"
ok "数据目录：$GW_DATA_HOST"

# ---------- 5. 启动 ----------
step 6/6 "启动服务"

# 配置变了（端口/镜像/tokenizer）就要重建容器才生效
if container_exists; then
  USED_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$(gw_container)" 2>/dev/null || echo '')"
  if [ "$USED_IMAGE" != "$IMAGE" ]; then
    docker rm -f "$(gw_container)" >/dev/null 2>&1 || true
  fi
fi

./stop.sh >/dev/null 2>&1 || true
if ./start.sh; then
  echo
  echo "============================================"
  echo "  安装完成，可以直接用了"
  echo "============================================"
  echo
  echo "  接下来在页面上："
  echo "    1. Base URL 填你的模型服务地址（如 http://192.168.1.10:8000）"
  echo "    2. 选接口类型和数据集，点「提交测试」"
  echo
  echo "  常用命令："
  echo "    ./start.sh     启动      ./stop.sh      停止"
  echo "    ./status.sh    查看状态  ./run.sh       前台运行（看日志）"
  echo "    docker logs -f $(gw_container)           查看实时日志"
  echo
else
  die "网关启动失败，请查看：docker logs $(gw_container)"
fi
