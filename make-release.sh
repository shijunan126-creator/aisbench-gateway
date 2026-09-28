#!/usr/bin/env bash
# ============================================================
#  开发侧打包脚本（不随交付包分发）
#
#  产出：aisbench-gateway-<版本>.tar.gz
#        客户解压后跑 ./install.sh 即可
#
#    ./make-release.sh                      用默认版本号打包
#    ./make-release.sh --version 1.1        指定版本
#    ./make-release.sh --split 1900M        分片（U 盘/FAT32 传输用）
#    ./make-release.sh --datasets gsm8k,ceval
#    ./make-release.sh --no-image           只打代码（镜像让客户自行 pull）
# ============================================================
set -uo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"

VERSION="1.0"
OUT_DIR="$ROOT/dist"
SPLIT_SIZE=""
DATASET_LIST="gsm8k,mmlu,ceval,cmmlu,math,gpqa,humaneval,mbpp"
WITH_IMAGE=1
IMAGE=""
# 交付目标平台。实际部署是 arm64 昇腾/鲲鹏机器，所以默认打 arm64。
#
# **必须显式指定并校验**：`docker save` 一个多平台 tag 会产出「混合」tar ——
# index.json 是 manifest list（containerd 存储按平台挑），而 manifest.json 只写
# 打包机那一个平台（经典/overlay2 存储的 docker load 只读它）。不收敛的话，
# 在 x86 开发机上打出来的包拿到 arm64 客户机、且对方 Docker 是经典存储时，
# load 出来的是 amd64 镜像 → docker run 平台不匹配 → 网关起不来。
PLATFORM="linux/arm64"

while [ $# -gt 0 ]; do
  case "$1" in
    --version)  VERSION="${2:-1.0}"; shift ;;
    --out)      OUT_DIR="${2:-$ROOT/dist}"; shift ;;
    --split)    SPLIT_SIZE="${2:-}"; shift ;;
    --datasets) DATASET_LIST="${2:-}"; shift ;;
    --image)    IMAGE="${2:-}"; shift ;;
    --platform) PLATFORM="${2:-}"; shift ;;
    --no-image) WITH_IMAGE=0 ;;
    -h|--help)  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

# 校验导出的 tar 是否自洽：manifest 里引用的每个 blob 都必须真实存在于包内。
#
# 必须做这一步的原因：**docker save 在本地层数据缺失时会静默产出不完整的包**
# （退出码 0、大小看着也正常），直到客户机器上 docker load 才报
#   failed to extract layer ... content digest ... not found
# 本机因为已有那些层，反而看不出来，所以只能靠结构校验兜住。
#
# 只读 tar，不动本地镜像，安全。
verify_image_tar() {
  python3 - "$1" <<'PYEOF'
import json, sys, tarfile, posixpath

path = sys.argv[1]
try:
    tf = tarfile.open(path, "r:")
except Exception as e:
    print(f"无法打开 tar: {e}")
    sys.exit(1)

names = set()
try:
    for m in tf.getmembers():
        names.add(m.name.lstrip("./"))
except Exception as e:
    print(f"读取 tar 条目失败: {e}")
    sys.exit(1)

def blob_ok(digest):
    """OCI 布局 blobs/sha256/<hex>；旧 docker 布局 <hex>/layer.tar。"""
    hexd = digest.split(":", 1)[1] if ":" in digest else digest
    return (f"blobs/sha256/{hexd}" in names) or (f"{hexd}/layer.tar" in names)

# 顺带把架构读出来（config blob 里有）。交付目标是 arm64，打成 amd64 的包
# 在 x86 开发机上自测完全"正常"，只有报出架构才能一眼看出搞错了。
arch = ""
try:
    man = json.load(tf.extractfile("manifest.json"))[0]
    cfgpath = man.get("Config", "").lstrip("./")
    if cfgpath in names:
        arch = json.load(tf.extractfile(cfgpath)).get("architecture", "")
except Exception:
    pass

missing = []
refs = 0

# 旧格式：顶层 manifest.json，含 Layers 列表（形如 "<hex>/layer.tar"）
if "manifest.json" in names:
    try:
        data = json.load(tf.extractfile("manifest.json"))
        for entry in data:
            for lay in entry.get("Layers", []):
                refs += 1
                if lay.lstrip("./") not in names:
                    missing.append(lay)
    except Exception as e:
        print(f"解析 manifest.json 失败: {e}")
        sys.exit(1)

# OCI 格式：index.json -> manifest -> layers
if "index.json" in names:
    try:
        idx = json.load(tf.extractfile("index.json"))
        for md in idx.get("manifests", []):
            d = md.get("digest", "")
            hexd = d.split(":", 1)[1] if ":" in d else d
            blob = f"blobs/sha256/{hexd}"
            if blob not in names:
                missing.append(blob)
                continue
            man = json.load(tf.extractfile(blob))
            for lay in man.get("layers", []):
                refs += 1
                if not blob_ok(lay.get("digest", "")):
                    missing.append(lay.get("digest", "?"))
            cfg = man.get("config", {}).get("digest")
            if cfg:
                refs += 1
                if not blob_ok(cfg):
                    missing.append(cfg)
    except Exception as e:
        print(f"解析 index.json 失败: {e}")
        sys.exit(1)

if refs == 0:
    print("没在 tar 里找到任何层引用，格式无法识别")
    sys.exit(1)

if missing:
    print(f"引用了 {refs} 个对象，其中 {len(missing)} 个在包内缺失，例如：")
    for m in missing[:3]:
        print(f"  {m}")
    sys.exit(1)

# 架构也要报出来：交付目标是 arm64，打成 amd64 的话在客户机上是平台不匹配、
# 容器根本起不来，而那种包在 x86 开发机上自测是"正常"的，光看自洽看不出来。
print(f"自洽：{refs} 个对象全部存在于包内（架构 {arch or '未知'}）")
sys.exit(0)
PYEOF
}

host_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo amd64 ;;
    aarch64|arm64) echo arm64 ;;
    *) uname -m ;;
  esac
}

# "linux/arm64" → "arm64"
plat_arch() { echo "${1##*/}"; }

# 把 docker save 出来的 tar 收敛成单平台（就地替换）。
#
# 为什么非做不可：docker save 一个多平台 tag 会产出「混合」tar ——
#   index.json    是 manifest list（含 amd64 + arm64），containerd 存储读它、按平台挑
#   manifest.json 只写**打包机那一个平台**，经典/overlay2 存储的 docker load 只读它
# 于是同一个包在不同客户机上 load 出不同架构，取决于对方的 Docker 版本。
# 裁成单平台后两条路径结果一致，跟 Docker 版本无关，包还小一半。
#
# 注意 annotations 必须原样带过去：containerd 路径下镜像名/tag 靠它恢复，
# 丢了的话 load 只会打印 "Loaded image ID"，install.sh 按 tag 找不到镜像。
to_single_platform() {
  local tar="$1" plat="$2" arch tmp
  arch="$(plat_arch "$plat")"
  tmp="${tar}.single"
  python3 - "$tar" "$tmp" "$arch" <<'PYEOF'
import io, json, shutil, sys, tarfile

src, dst, arch = sys.argv[1], sys.argv[2], sys.argv[3]

with tarfile.open(src, "r:") as ti:
    members = {m.name: m for m in ti.getmembers()}
    if "index.json" not in members:
        sys.exit("index.json 不存在（不是 docker save 的产物？）")
    idx = json.load(ti.extractfile("index.json"))
    lst_desc = idx["manifests"][0]
    annotations = lst_desc.get("annotations", {})
    lst = json.load(ti.extractfile("blobs/sha256/" + lst_desc["digest"].split(":")[1]))
    if "manifests" not in lst:
        # docker 经典存储（overlay2）下，本地 tag 只存**一个**平台：
        # `--platform linux/arm64` 拉的镜像 save 出来就是单 manifest 而不是
        # manifest list —— 这正是交付想要的形态，不该当失败处理。
        # 这里读出它的架构，和目标一致就原样通过（tar 不动，交给后面的
        # verify_image_tar 做完整性校验）；不一致才是真的平台错误。
        cfg_digest = (lst.get("config") or {}).get("digest", "")
        if not cfg_digest:
            sys.exit("单 manifest tar 里读不到 config digest")
        cfg = json.load(ti.extractfile("blobs/sha256/" + cfg_digest.split(":", 1)[1]))
        arch_now = cfg.get("architecture", "?")
        if arch_now == arch:
            # 已经是目标的单平台：调用方靠「dst 文件存在」判断成功，
            # 所以原样复制一份再退出，tar 内容不动
            shutil.copyfile(src, dst)
            sys.exit(0)
        sys.exit(f"tar 是单平台 {arch_now}，与目标 {arch} 不符")

    target = next((s for s in lst["manifests"]
                   if s.get("platform", {}).get("architecture") == arch), None)
    if target is None:
        have = ",".join(s.get("platform", {}).get("architecture", "?") for s in lst["manifests"])
        sys.exit(f"包里没有 {arch}（只有 {have}）")

    img = target["digest"].split(":")[1]
    sub = json.load(ti.extractfile("blobs/sha256/" + img))
    cfg = sub["config"]["digest"].split(":")[1]
    layers = [l["digest"].split(":")[1] for l in sub["layers"]]
    keep = {f"blobs/sha256/{h}" for h in [img, cfg] + layers}

    missing = [k for k in keep if k not in members]
    if missing:
        sys.exit(f"{arch} 有 {len(missing)} 个 blob 不在包里")

    old = json.load(ti.extractfile("manifest.json"))[0]
    new_manifest = json.dumps([{
        "Config": f"blobs/sha256/{cfg}",
        "RepoTags": old.get("RepoTags", []),
        "Layers": [f"blobs/sha256/{h}" for h in layers],
    }], indent=2).encode()

    new_index = json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [{
            "mediaType": target.get("mediaType"),
            "digest": f"sha256:{img}",
            "size": members[f"blobs/sha256/{img}"].size,
            "annotations": annotations,
            "platform": {"architecture": arch, "os": "linux"},
        }],
    }, indent=2).encode()

    ti.offset = 0
    with tarfile.open(dst, "w:", format=tarfile.GNU_FORMAT) as to:
        for m in ti:
            if m.name == "manifest.json":
                m.size = len(new_manifest); to.addfile(m, io.BytesIO(new_manifest))
            elif m.name == "index.json":
                m.size = len(new_index); to.addfile(m, io.BytesIO(new_index))
            elif m.name.startswith("blobs/sha256/") and m.isfile():
                if m.name in keep:
                    to.addfile(m, ti.extractfile(m))
            else:
                to.addfile(m, ti.extractfile(m) if m.isfile() else None)
PYEOF
  if [ $? -ne 0 ] || [ ! -s "$tmp" ]; then
    rm -f "$tmp"
    return 1
  fi
  mv -f "$tmp" "$tar"
  return 0
}

PKG_NAME="aisbench-gateway-${VERSION}"
STAGE="$(mktemp -d)"
PKG="$STAGE/$PKG_NAME"
mkdir -p "$PKG"

cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

echo "打包 AISBench 网关 v${VERSION}"
echo "  输出目录：$OUT_DIR"

# ---------- 取镜像 ----------
if [ "$WITH_IMAGE" = "1" ]; then
  step "导出 aisbench 镜像"

  # 注意别写成 "${IMAGE:-$("cmd 'arg' || echo "")"}" —— 四层嵌套引号会让 bash 解析失败。
  # 分两步写，既安全又好读。
  if [ -z "$IMAGE" ]; then
    IMAGE="$(python3 -c 'from gw.settings import settings; print(settings.image)' 2>/dev/null || true)"
  fi
  [ -z "$IMAGE" ] && { echo "错误：拿不到镜像名，请用 --image 指定" >&2; exit 1; }
  echo "  镜像：$IMAGE"

  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "  本地没有这个镜像，先拉取…"
    docker pull "$IMAGE" || { echo "错误：拉取失败" >&2; exit 1; }
  fi

  mkdir -p "$PKG/images"
  TAR="$PKG/images/aisbench_benchmark.tar"

  # 每个方案都要：导出 → 结构校验。校验不通过就换下一个方案。
  # 校验是必须的 —— 本机的镜像层数据有缺失，docker save 会静默产出不完整的包。
  try_export() {
    local how="$1"
    rm -f "$TAR"
    case "$how" in
      save)
        docker save -o "$TAR" "$IMAGE" 2>/tmp/exp.err || return 1
        ;;
      commit)
        # docker commit 会依据容器的实际文件系统生成一份**完整的**新镜像，
        # 不受本地层元数据损坏的影响。用全新容器（不启动、不额外 -e/-v）提交，
        # 避免把运行时的环境变量烧进去。
        # 目标 tag 直接写原镜像名，客户导入后的名字就和 config.ini 里的一致。
        local tmpc="aisbench-pack-tmp-$$"
        docker rm -f "$tmpc" >/dev/null 2>&1 || true
        docker create --name "$tmpc" "$IMAGE" >/dev/null 2>&1 || { warn "创建临时容器失败"; return 1; }
        docker commit --change 'CMD ["/bin/bash"]' "$tmpc" "$IMAGE" >/dev/null 2>&1 \
          || { warn "commit 失败"; docker rm -f "$tmpc" >/dev/null 2>&1; return 1; }
        docker rm -f "$tmpc" >/dev/null 2>&1 || true
        docker save -o "$TAR" "$IMAGE" 2>/tmp/exp.err || return 1
        ;;
      pull)
        # 显式指定平台：不带 --platform 时默认拉宿主机架构，在 x86 开发机上
        # 打 arm64 包就会拉到 amd64
        docker pull --platform "$PLATFORM" "$IMAGE" || return 1
        docker save -o "$TAR" "$IMAGE" 2>/tmp/exp.err || return 1
        ;;
    esac
    [ -s "$TAR" ] || { warn "导出的文件为空"; return 1; }
    return 0
  }

  EXPORTED=""
  for how in save commit pull; do
    case "$how" in
      save)   echo "  [1/3] docker save …" ;;
      commit) echo "  [2/3] docker commit 后导出（本地生成完整镜像）…" ;;
      pull)   echo "  [3/3] docker pull 后导出（需要联网，可能要下几个 GB）…" ;;
    esac

    # docker commit 造出来的镜像**只有打包机这一个架构**。目标是别的架构时
    # 这条路必然产出错平台的包，直接跳过，别让它污染交付物。
    if [ "$how" = "commit" ] && [ "$(plat_arch "$PLATFORM")" != "$(host_arch)" ]; then
      warn "跳过 commit 方案：它只会生成当前机器架构（$(host_arch)）的镜像，而目标是 $PLATFORM"
      continue
    fi

    if ! try_export "$how"; then
      warn "导出失败：$(head -1 /tmp/exp.err 2>/dev/null)"
      continue
    fi
    ok "导出完成（$(du -h "$TAR" | cut -f1)）"

    echo "        收敛为单平台（$PLATFORM）…"
    if ! to_single_platform "$TAR" "$PLATFORM"; then
      warn "包里没有 $PLATFORM 这个平台，换下一种导出方式"
      continue
    fi
    ok "已是单平台：$PLATFORM（$(du -h "$TAR" | cut -f1)）"

    echo "        校验包完整性 …"
    if VERIFY_MSG="$(verify_image_tar "$TAR")"; then
      ok "校验通过 —— $VERIFY_MSG"
      EXPORTED="$how"
      break
    else
      warn "校验不通过：$VERIFY_MSG"
      warn "（本机层数据不完整，这种包到客户机器上 docker load 会失败）"
      rm -f "$TAR"
    fi
  done

  if [ -z "$EXPORTED" ]; then
    echo "错误：三种方式都无法导出**完整**的镜像" >&2
    echo "  最后错误：$(head -1 /tmp/exp.err 2>/dev/null)" >&2
    echo "  退路：用 --no-image 只打代码，让客户在有外网的机器上自行 docker pull" >&2
    exit 1
  fi

  # 结构完整 ≠ 能跑。真载入一遍并跑一次 ais_bench，这是唯一能确保
  # 客户现场不出问题的方法。载入会覆盖本地同名镜像，但内容就是我们刚导出的，
  # 载入成功等于原地换成等价镜像，没有损失。
  echo "        实际载入并运行验证 …"
  LOAD_OUT="$(docker load -i "$TAR" 2>&1)"
  if echo "$LOAD_OUT" | grep -qE "Error unpacking|failed to extract layer|not found"; then
    echo "错误：导出的包无法完整载入，不能交付" >&2
    echo "$LOAD_OUT" | tail -3 | sed 's/^/    /' >&2
    echo "  处理：检查磁盘空间；或先 docker rmi 掉本地镜像再从仓库 docker pull 一次，" >&2
    echo "        然后重新执行本脚本。" >&2
    exit 1
  fi
  if docker run --rm --entrypoint ais_bench "$IMAGE" --help >/dev/null 2>&1; then
    ok "载入后 ais_bench 可正常运行"
  else
    echo "错误：包能载入但 ais_bench 跑不起来，不能交付" >&2
    echo "  手动复现：docker run --rm --entrypoint ais_bench $IMAGE --help" >&2
    exit 1
  fi

  # 完整性已在上面逐个方案校验过，这里不再重复
else
  step "跳过镜像（--no-image）"
  warn "客户需要自己在能上网的机器上 docker pull"
fi

# ---------- 代码与脚本 ----------
step "收集代码与脚本"

# lib.sh 是各脚本共用的函数库，必须一起带上
for f in lib.sh install.sh start.sh stop.sh status.sh run.sh; do
  [ -f "$f" ] && cp "$f" "$PKG/" && chmod +x "$PKG/$f"
done
ok "运维脚本（含 lib.sh）"

# 刻意**不**打包 tools/ —— 里面是开发自测用的假模型服务。
# 把假服务放进一个做基准测试的交付物里，风险是被误当成真实结果来源。
# 客户手上只应有真实测试工具。开发自测请用仓库里的 tools/。
for d in gw static; do
  rsync -a --exclude '__pycache__' --exclude '*.pyc' "$d/" "$PKG/$d/" 2>/dev/null \
    || cp -r "$d" "$PKG/"
done
ok "代码与前端资源（gw/ static/）"
warn "已排除 tools/（假模型服务仅供开发自测，不随交付包分发）"

# 清掉不该进的
find "$PKG" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$PKG" -name '*.pyc' -delete 2>/dev/null || true

# ---------- 数据集 ----------
step "打包预置数据集"

SEED_SRC="data/datasets/ais_bench/datasets"
if [ -n "$DATASET_LIST" ] && [ -d "$SEED_SRC" ]; then
  mkdir -p "$PKG/seed/ais_bench/datasets"
  IFS=',' read -ra NAMES <<< "$DATASET_LIST"
  TOTAL=0
  for n in "${NAMES[@]}"; do
    n="$(echo "$n" | xargs)"
    [ -z "$n" ] && continue
    SRC=""
    # 有些数据集的目录名与 family 名不同（如 aime2024 -> aime）。
    # 这个兜底**必须和名字绑定**：以前它是无条件候选，于是任何本地没有的名字
    # 都会被静默解析成 aime 打进包里，manifest 里还照样声称包含那个数据集。
    for cand in "$SEED_SRC/$n" "$SEED_SRC/${n}_simple_eval"; do
      [ -d "$cand" ] && { SRC="$cand"; break; }
    done
    if [ -z "$SRC" ] && [ "$n" = "aime2024" ] && [ -d "$SEED_SRC/aime" ]; then
      SRC="$SEED_SRC/aime"
    fi
    if [ -n "$SRC" ]; then
      cp -r "$SRC" "$PKG/seed/ais_bench/datasets/" && {
        echo "      + $(basename "$SRC")  ($(du -sh "$SRC" | cut -f1))"
        TOTAL=$((TOTAL+1))
      }
    else
      warn "跳过 $n（本地没有）"
    fi
  done
  ok "共 $TOTAL 个数据集（$(du -sh "$PKG/seed" | cut -f1)）"
else
  warn "没有可打包的数据集"
fi

# ---------- config.ini 模板 ----------
step "生成配置模板"
python3 - "$PKG/config.ini" "${IMAGE:-}" <<'PYEOF'
import sys
from pathlib import Path
from gw.settings import TEMPLATE, DEFAULT_IMAGE

out = Path(sys.argv[1])
image = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else DEFAULT_IMAGE
out.write_text(TEMPLATE.format(image=image), encoding="utf-8")
print(f"      {out}")
PYEOF
ok "config.ini（tokenizer_dirs 留空，由客户在安装时或页面里指定）"

# ---------- 元信息 ----------
step "写入元信息"

echo "$VERSION" > "$PKG/VERSION"

python3 - "$PKG/manifest.json" "$VERSION" "${IMAGE:-}" "$DATASET_LIST" "$(plat_arch "$PLATFORM")" <<'PYEOF'
import json, sys, time
from pathlib import Path

out, version, image, datasets, arch = sys.argv[1:6]
pkg = Path(out).parent

def dsize(p):
    p = Path(p)
    if not p.exists():
        return 0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())

manifest = {
    "name": "aisbench-gateway",
    "version": version,
    "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "image": image,
    "image_tar": "images/aisbench_benchmark.tar",
    "datasets": [d.strip() for d in datasets.split(",") if d.strip()],
    "requires": {
        "docker": "20.10+",
        "python": "3.8+",
        "python_packages": [],          # 网关零第三方依赖
        # 包里的镜像已被裁成**单平台**（见 make-release.sh 的 to_single_platform），
        # install.sh 会拿这个值和本机架构对一下，不一致就明确报错
        "arch": arch,
    },
    "entrypoint": "./install.sh",
}
Path(out).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"      manifest.json  version={version}")
PYEOF

# 客户版 README
if [ -f README.customer.md ]; then
  cp README.customer.md "$PKG/README.md"
else
  echo "错误：缺少 README.customer.md" >&2
  exit 1
fi
ok "README.md"

# ---------- 打包 ----------
step "生成压缩包"

mkdir -p "$OUT_DIR"
ARCHIVE="$OUT_DIR/${PKG_NAME}.tar.gz"
rm -f "$ARCHIVE"

echo "  正在压缩（镜像未压缩放入，外层统一 gzip）…"
if tar -C "$STAGE" -cf - "$PKG_NAME" | gzip -1 > "$ARCHIVE"; then
  ok "已生成 $(basename "$ARCHIVE")  ($(du -h "$ARCHIVE" | cut -f1))"
else
  echo "错误：压缩失败" >&2
  exit 1
fi

# ---------- 分片 ----------
if [ -n "$SPLIT_SIZE" ]; then
  step "分片（每片 $SPLIT_SIZE）"
  ( cd "$OUT_DIR" && split -b "$SPLIT_SIZE" -d -a 2 "$(basename "$ARCHIVE")" "$(basename "$ARCHIVE").part-" )
  rm -f "$ARCHIVE"
  ok "已分片："
  ls -1 "$OUT_DIR"/"$(basename "$ARCHIVE")".part-* | sed 's/^/      /'
  echo
  echo "  合并方式： cat $(basename "$ARCHIVE").part-* > $(basename "$ARCHIVE")"
fi

# ---------- 校验和 ----------
step "生成校验和"
( cd "$OUT_DIR" && sha256sum ${PKG_NAME}.tar.gz* > SHA256SUMS 2>/dev/null ) || true
ok "SHA256SUMS"

echo
echo "============================================"
echo "  打包完成"
echo "============================================"
ls -lh "$OUT_DIR" | tail -n +2 | awk '{printf "  %-45s %s\n", $NF, $5}'
echo
echo "  客户侧：解压后执行 ./install.sh"
