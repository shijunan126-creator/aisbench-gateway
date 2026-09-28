"""配置加载（仅用标准库）。

用 `configparser` 读写 `config.ini`，**不依赖 PyYAML**。

两条设计约束：

1. **零第三方依赖** —— 交付包要在客户机上「解压即用」，而 PyYAML / pydantic
   这类包在 PyPI 上是**平台相关**的 wheel（`...manylinux...x86_64.whl`），
   客户机架构或 Python 版本对不上就装不上。用标准库彻底绕开这个问题。

2. **网关跑在容器里** —— 所以这里所有路径都是**容器内路径**，
   数据目录固定是 `/work`（start.sh 把宿主机的 `<包目录>/data` 挂到那里）。
   代码里不再需要「宿主机路径 ↔ 容器内路径」的转换。

最低支持 Python 3.8（所有模块都有 `from __future__ import annotations`）。
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

# 包目录。容器内是 /gateway（gw/、static/、config.ini 都在这下面）
ROOT = Path(__file__).resolve().parent.parent

DEFAULT_IMAGE = (
    "swr.cn-south-1.myhuaweicloud.com/ascendhub/"
    "aisbench_benchmark:v3.1-20260630-master-ubuntu22.04-py310"
)

# 容器内的数据目录，start.sh 把宿主机的 <包目录>/data 挂到这里。
# 不要改 —— 生成的 mmengine 配置里写的就是这个路径。
DEFAULT_DATA = "/work"

TEMPLATE = """\
# AISBench 网关配置
#
# 改完这个文件后需要重启网关才生效：
#   ./stop.sh && ./start.sh
#
# 注意：本文件是给**容器内**的网关读的，所以路径都是容器内路径。
# 数据目录固定为 /work，由 start.sh 把宿主机上的 <本包目录>/data 挂过去。

[server]
# 监听地址。0.0.0.0 表示所有网卡，局域网内其他机器也能访问
host = 0.0.0.0
port = 8080

[aisbench]
# aisbench 镜像。升级时改这里的 tag，然后 ./stop.sh && ./start.sh
# （start.sh 会发现镜像变了并重建容器）
image = {image}
# 网关容器名
container = aisbench-gateway

# 容器内的数据目录。start.sh 把宿主机的 <包目录>/data 挂到这里，**不要改**
# ——生成的 mmengine 配置里写死了这个路径。
data = /work

# 模型/tokenizer 目录，多个用逗号分隔。填**宿主机**上的路径，
# 由 start.sh 按原路径只读挂进容器。可以填模型目录本身，也可以填装了
# 多个模型的**父目录**（如 /data/models），网关会扫描其中的模型文件夹，
# 页面上按目录名选择即可。
# 随机数据集(synthetic)、sharegpt 和 GSM8K 前缀数据集**必须**提供，
# 而镜像里不带任何 tokenizer（install.sh 安装时会强制要求填写）。
# 也可以把 tokenizer 直接放进 <包目录>/data/models/，用 /work/models/<名字> 引用。
tokenizer_dirs =

[metrics]
# 「采集 prefix cache 命中率」时去查哪些 /metrics 端点，逗号分隔，形如 ip:port。
#
# 留空 = 用被测服务自己的地址（base_url 的 host:port）。PD 混部填这个就够，
# 大多数情况不需要改这里。
# PD 分离时，填各个 **P 节点**的 IP 和它对应 DP 域的端口。
# 查不到不会让任务失败，只会在结果里标注跳过原因。
pods =

[runner]
# 压测任务串行执行，避免多个任务互相抢模型服务污染吞吐数据
max_concurrent_jobs = 1
# 单个任务的硬超时（秒），0 表示不限制
job_timeout = 0
# 无输出看门狗（秒）：日志超过这么久没有新增就判定卡死并终止。
# aisbench 在某些环境下会偶发死锁（请求已发完但进程不退出），
# 单 worker 串行时这种任务会把整个队列堵死，所以需要兜底。0 表示关闭。
stall_timeout = 900

[defaults]
# 前端表单的初始值
model =
max_out_len = 512
concurrency = 8
num_prompts = 100
num_warmups = 1
temperature = 0.01
ignore_eos = true
"""


def _split_list(raw: str) -> List[str]:
    if not raw:
        return []
    out: List[str] = []
    for chunk in raw.replace("\n", ",").split(","):
        v = chunk.strip().strip('"').strip("'")
        if v:
            out.append(v)
    return out


def _as_int(v: Any, default: int) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 8080

    # 这两个是给 shell 脚本读的（脚本用 awk 解析 config.ini），网关自己不用
    image: str = DEFAULT_IMAGE
    container: str = "aisbench-gateway"
    tokenizer_dirs: List[str] = field(default_factory=list)

    # 采集 prefix cache 命中率时查的 /metrics 端点。空 = 用被测服务自己（见 metrics.py）
    metrics_pods: List[str] = field(default_factory=list)

    data_dir: Path = field(default_factory=lambda: Path(DEFAULT_DATA))
    max_concurrent_jobs: int = 1
    job_timeout: int = 0
    stall_timeout: int = 900

    defaults: Dict[str, Any] = field(default_factory=dict)

    config_path: Path = field(default_factory=lambda: ROOT / "config.ini")

    # ---- 派生路径（都是容器内路径） ----
    @property
    def data_path(self) -> str:
        """数据目录的字符串形式，用于拼 work_dir。"""
        return str(self.data_dir).rstrip("/")

    @property
    def models_dir(self) -> Path:
        """本地 tokenizer 存放目录。"""
        return self.data_dir / "models"

    @property
    def configs_dir(self) -> Path:
        return self.data_dir / "configs"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def datasets_dir(self) -> Path:
        """对应 aisbench 的 AIS_BENCH_DATASETS_CACHE。"""
        return self.data_dir / "datasets"

    @property
    def generated_dir(self) -> Path:
        """按长度/前缀比例合成出来的数据集（见 dataset_gen.py）。

        放在数据目录下而不是临时目录：生成长上下文数据集可能要几分钟，
        用户会想在多次运行之间复用，也得能自己翻出来看。
        """
        return self.data_dir / "generated"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "gateway.db"

    def ensure_dirs(self) -> None:
        for d in (self.configs_dir, self.outputs_dir, self.datasets_dir,
                  self.models_dir, self.generated_dir):
            d.mkdir(parents=True, exist_ok=True)


def write_template(path: Path, image: str = DEFAULT_IMAGE) -> None:
    path.write_text(TEMPLATE.format(image=image), encoding="utf-8")


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    cfg_path = Path(path) if path else (ROOT / "config.ini")

    if not cfg_path.exists():
        write_template(cfg_path)

    cp = configparser.ConfigParser(interpolation=None)  # 关掉 % 插值，避免路径里带 % 报错
    cp.read(str(cfg_path), encoding="utf-8")

    def get(section: str, key: str, default: str = "") -> str:
        if cp.has_section(section) and cp.has_option(section, key):
            return cp.get(section, key).strip()
        return default

    data_raw = get("aisbench", "data") or DEFAULT_DATA
    data_dir = Path(data_raw)

    defaults: Dict[str, Any] = {}
    if cp.has_section("defaults"):
        for k, v in cp.items("defaults"):
            v = v.strip()
            if v.lower() in ("true", "false"):
                defaults[k] = v.lower() == "true"
            else:
                try:
                    defaults[k] = int(v)
                except ValueError:
                    try:
                        defaults[k] = float(v)
                    except ValueError:
                        defaults[k] = v

    s = Settings(
        host=get("server", "host", "0.0.0.0") or "0.0.0.0",
        port=_as_int(get("server", "port", "8080"), 8080),
        image=get("aisbench", "image", DEFAULT_IMAGE) or DEFAULT_IMAGE,
        container=get("aisbench", "container", "aisbench-gateway") or "aisbench-gateway",
        tokenizer_dirs=_split_list(get("aisbench", "tokenizer_dirs", "")),
        metrics_pods=_split_list(get("metrics", "pods", "")),
        data_dir=data_dir,
        max_concurrent_jobs=max(1, _as_int(get("runner", "max_concurrent_jobs", "1"), 1)),
        job_timeout=max(0, _as_int(get("runner", "job_timeout", "0"), 0)),
        stall_timeout=max(0, _as_int(get("runner", "stall_timeout", "900"), 900)),
        defaults=defaults,
        config_path=cfg_path,
    )

    # 启动时数据目录可能还没挂上（比如没按 start.sh 起容器），
    # 这里不抛异常，交给 runtime.selfcheck 去报告，错误信息更清楚。
    try:
        s.ensure_dirs()
    except OSError:
        pass
    return s


def save_settings(s: Settings) -> None:
    """把当前配置写回 config.ini。"""
    cp = configparser.ConfigParser(interpolation=None)
    if s.config_path.exists():
        cp.read(str(s.config_path), encoding="utf-8")

    def put(section: str, key: str, val: str) -> None:
        if not cp.has_section(section):
            cp.add_section(section)
        cp.set(section, key, val)

    put("server", "host", s.host)
    put("server", "port", str(s.port))
    put("aisbench", "image", s.image)
    put("aisbench", "container", s.container)
    put("aisbench", "data", s.data_path)
    put("aisbench", "tokenizer_dirs", ", ".join(s.tokenizer_dirs))
    put("metrics", "pods", ", ".join(s.metrics_pods))
    put("runner", "max_concurrent_jobs", str(s.max_concurrent_jobs))
    put("runner", "job_timeout", str(s.job_timeout))
    put("runner", "stall_timeout", str(s.stall_timeout))

    with s.config_path.open("w", encoding="utf-8") as f:
        cp.write(f)


settings = load_settings()
