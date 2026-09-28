"""把前端表单翻译成 aisbench 能吃的 mmengine 配置文件。

生成的是一份**自包含**配置，用位置参数传给 CLI：

    ais_bench /work/configs/<job_id>/run.py -m perf --num-prompts 100

为什么不用 `--models X --datasets Y`：那条路要求配置文件落在 `--config-dir` 的
`models/` `datasets/` 子树里，而且镜像自带的示例配置用的是相对导入
（`from .vllm_api_stream_chat import models`），一旦挪出原目录就加载失败。
自包含配置没有这个约束。

另外镜像里的 `configs/api_examples/*.py` 已经**过时**（引用了不存在的
`ais_bench.benchmark.runners.local_api`），不要照抄那些文件。
当前 runner 在 `ais_bench.benchmark.runners.local`。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

from .catalog import API_TYPES_BY_ID, PREFIX_GEN_FAMILY, get_dataset
from .settings import Settings

# 精度 / 性能两种模式要用不同的 summarizer
SUMMARIZER_ACCURACY = "ais_bench.benchmark.configs.summarizers.example"
SUMMARIZER_PERF = "ais_bench.benchmark.configs.summarizers.perf.default_perf"


def parse_base_url(base_url: str, api_id: str) -> Dict[str, Any]:
    """把用户填的 base_url 拆成模型的连接参数。

    **这里有个必须绕开的坑**：模型内部是 `urljoin(base, "v1/chat/completions")`。
    如果直接把 `http://h:8000/v1` 塞进 `url`，拼出来会变成
    `http://h:8000/v1/v1/chat/completions`（见 models/api_models/base_api.py::_get_base_url）。

    所以：去掉结尾的 `/v1`；路径清空后用 host_ip/host_port，有自定义前缀才用 url。
    """
    api = API_TYPES_BY_ID[api_id]
    raw = (base_url or "").strip()
    if not raw:
        raise ValueError("base_url 不能为空")
    if "://" not in raw:
        raw = "http://" + raw

    u = urlparse(raw)
    if not u.hostname:
        raise ValueError(f"无法解析 base_url: {base_url}")

    scheme = (u.scheme or "http").lower()
    port = u.port or (443 if scheme == "https" else 80)

    # 去掉结尾的 /v1（以及可能的 /v1/），避免拼出 /v1/v1/...
    path = (u.path or "").rstrip("/")
    path = re.sub(r"/v1$", "", path).rstrip("/")

    out: Dict[str, Any] = {
        "enable_ssl": scheme == "https",
        "stream": bool(api.get("stream", False)),
    }
    if path:
        # 有自定义前缀，只能用 url；注意此时 host_ip/host_port 会被忽略
        hostpart = u.hostname
        if ":" in hostpart:  # IPv6 字面量
            hostpart = f"[{hostpart}]"
        out["url"] = f"{scheme}://{hostpart}:{port}{path}"
    else:
        out["url"] = ""
        # 模型只接受 IPv4/IPv6/localhost，其余交给 url 分支
        try:
            import ipaddress

            ipaddress.ip_address(u.hostname)
            out["host_ip"] = u.hostname
        except ValueError:
            out["host_ip"] = u.hostname  # localhost 或域名
        out["host_port"] = port
    return out


def _py(value: Any) -> str:
    """把 Python 值渲染成配置源码里的字面量。"""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        inner = ", ".join(f"{json.dumps(str(k))}: {_py(v)}" for k, v in value.items())
        return "{" + inner + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_py(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


# 生成的数据集每行长这样，schema 由 dataset_gen 完全掌控
_GEN_INPUT_COLUMNS = ["question", "max_out_len"]
_GEN_OUTPUT_COLUMN = "answer"


def _custom_dataset_config(path: str, abbr: str) -> Dict[str, Any]:
    """构造 CustomDataset 的配置字面量。

    **为什么在网关进程里构造、再把结果序列化进配置文件**：
    mmengine 的 `Config.fromfile` 是惰性导入，被 import 的名字会包成 LazyObject，
    在配置顶层调用它会直接抛 `RuntimeError`（`lazy.py:103`）。所以配置文件里
    不能出现函数调用，只能在网关这边算好、以字面量写进去。

    **为什么不直接用 `make_custom_dataset_config`**：它内部的
    `parse_example_dataset` 会真的去 open 数据集文件读第一行。而提交任务时
    我们只做一次配置预校验，那时数据集还没生成（生成在真正执行时才做），
    文件不存在就会失败。这里改为调它的下一层 `make_qa_gen_config`，
    meta 由我们自己填 —— 生成的数据集是我们写的，schema 完全已知：
    `question` 是正文、`answer` 是占位符、没有 A/B/C 选项，
    所以 `parse_example_dataset` 推出来的结果必然就是下面这些值。
    （`data_type` 判据是 `len(options) > 1`，我们没有 options，恒为 qa。）

    **template 必须显式给**：默认模板是
    `"Question: {question}\\nAnswer: {answer}"`，会把 answer 拼进 prompt。
    那样每条请求凭空多出 "Question: " 和 "\\nAnswer: none" 十几个 token，
    实测长度就不是用户指定的值了。给 `{question}` 才能让 prompt 正好是生成的文本。
    """
    from ais_bench.benchmark.datasets.custom import make_qa_gen_config, stringfy_types
    from ais_bench.benchmark.utils.logging.logger import AISLogger

    meta = {
        "path": path,
        "abbr": abbr,
        "input_columns": list(_GEN_INPUT_COLUMNS),
        "output_column": _GEN_OUTPUT_COLUMN,
        "options": [],
        "data_type": "qa",
        "infer_method": "gen",
        "meta_path": None,
        "template": "{question}",
    }
    return stringfy_types(make_qa_gen_config(meta, AISLogger()))


def _synthetic_override(p: Dict[str, Any]) -> Dict[str, Any]:
    """把 UI 的随机数据集参数映射成 synthetic_config 的形状。

    字段与取值范围见镜像内 ais_bench/datasets/synthetic/synthetic_config.py 的文档注释。
    """
    mode = p.get("synthetic_mode", "string")
    count = int(p.get("num_prompts") or p.get("request_count") or 100)

    cfg: Dict[str, Any] = {
        "Type": mode,
        "RequestCount": count,
        "TrustRemoteCode": False,
    }

    if mode == "tokenid":
        cfg["TokenIdConfig"] = {
            "RequestSize": int(p.get("input_len") or 1024),
            "PrefixLen": int(p.get("prefix_len") or 0),
        }
        # tokenid 模式需要本地 tokenizer 才能生成 token id
        cfg["TrustRemoteCode"] = bool(p.get("trust_remote_code", False))
        return cfg

    def _dist(kind: str, lo: int, hi: int) -> Dict[str, Any]:
        lo, hi = int(lo), int(hi)
        if hi < lo:
            lo, hi = hi, lo
        if kind == "gaussian":
            mean = (lo + hi) // 2
            var = max(1, (hi - lo) // 4)
            return {
                "Method": "gaussian",
                "Params": {"Mean": mean, "Var": var, "MinValue": lo, "MaxValue": hi},
            }
        if kind == "zipf":
            hi = max(hi, lo + 1)
            return {
                "Method": "zipf",
                "Params": {"Alpha": 1.2, "MinValue": lo, "MaxValue": hi},
            }
        return {"Method": "uniform", "Params": {"MinValue": lo, "MaxValue": hi}}

    cfg["StringConfig"] = {
        "Input": _dist(
            p.get("input_dist", "uniform"),
            p.get("input_len_min", p.get("input_len") or 1024),
            p.get("input_len_max", p.get("input_len") or 1024),
        ),
        "Output": _dist(
            p.get("output_dist", "uniform"),
            p.get("output_len_min", p.get("max_out_len") or 256),
            p.get("output_len_max", p.get("max_out_len") or 256),
        ),
    }
    return cfg


def build_config_source(
    job: Dict[str, Any],
    dataset_path: str | None = None,
    stage: str = "main",
    validate_only: bool = False,
) -> str:
    """按任务参数生成配置文件源码。

    `dataset_path` 只对生成的数据集有意义：预热阶段和正式阶段用同一份模型配置，
    只是数据集文件不同，靠这个参数切换。

    `stage` 决定 work_dir。预热阶段写进 `<任务目录>/warmup/`，这样
    `results.find_runs`（只认 `\\d{8}_\\d{6}` 子目录）不会把预热的产物
    当成正式结果，同时产物还留在任务目录下方便排查。

    `validate_only` 给提交时的预校验用（main.py::h_jobs_submit）。那时合成数据集
    还没生成，路径当然也没有，但校验要的是「接口类型/base_url/tokenizer 填对没有」，
    不需要真路径 —— 用占位符把配置拼出来即可，反正这份配置会被丢掉。
    真实执行时路径一定存在（runner 先生成再写配置），所以不会走到占位分支。
    """
    p = job.get("params") or {}
    mode = job.get("mode", "accuracy")  # accuracy | perf
    api_id = p.get("api_type", "chat_stream")
    api = API_TYPES_BY_ID.get(api_id)
    if not api:
        raise ValueError(f"未知接口类型: {api_id}")

    family = p["dataset"]
    ds = get_dataset(family)
    if not ds:
        raise ValueError(f"未知数据集: {family}")
    variant = p.get("variant") or ds["default_variant"]

    is_gen = family == PREFIX_GEN_FAMILY

    if is_gen and mode != "perf":
        raise ValueError(
            "生成的长度/前缀数据集只支持性能测试：它的 answer 是占位符，"
            "算出来的精度得分没有意义。"
        )

    # synthetic / sharegpt 必须提供本地 tokenizer 路径，否则 aisbench 在
    # cli/utils.py::fill_model_path_if_datasets_need 直接抛 AISBenchConfigError。
    # 提前拦下来，报错信息比容器里那串堆栈友好得多。
    #
    # 生成的数据集也要 tokenizer，但原因不同：它是在**网关这边生成时就**需要
    # tokenizer 来把文本调整到指定的 token 长度，跟 aisbench 无关。
    if family in ("synthetic", "sharegpt", PREFIX_GEN_FAMILY) and not (
        p.get("tokenizer_path") or ""
    ).strip():
        raise ValueError(
            f"数据集 {family} 需要本地 tokenizer 路径（模型配置的 path 字段）。"
            "请把 tokenizer 目录放进数据目录的 models/ 下（表单里填 /work/models/<目录名>），"
            "或在 config.ini 的 aisbench.tokenizer_dirs 里配置宿主机模型目录后重启网关。"
        )

    conn = parse_base_url(p.get("base_url", ""), api_id)

    model_cfg: Dict[str, Any] = {
        "attr": "service",
        "type": api["model_class"],
        "abbr": f"ui-{job['id']}",
        # path 是**本地 tokenizer 路径**，不是服务地址。synthetic/sharegpt 需要它做 token 计数，
        # 留空时 aisbench 会退化处理，但对随机长度数据集建议指向被压测模型的 tokenizer 目录。
        "path": p.get("tokenizer_path", "") or "",
        "model": p.get("model", "") or "",
        "stream": conn["stream"],
        "max_out_len": int(p.get("max_out_len") or 512),
        "retry": int(p.get("retry") or 2),
        "api_key": p.get("api_key", "") or "",
        "trust_remote_code": bool(p.get("trust_remote_code", False)),
        "generation_kwargs": {
            "temperature": float(p.get("temperature", 0.01)),
            "ignore_eos": bool(p.get("ignore_eos", True)),
        },
        # batch_size 就是并发数（见 tasks/openicl_api_infer.py: self.concurrency = batch_size）
        "batch_size": int(p.get("concurrency") or 1),
        "request_rate": int(p.get("request_rate") or 0),
    }
    if conn.get("url"):
        model_cfg["url"] = conn["url"]
    else:
        model_cfg["host_ip"] = conn.get("host_ip", "localhost")
        model_cfg["host_port"] = conn.get("host_port", 80)
    if api.get("returns_tool_calls"):
        model_cfg["returns_tool_calls"] = True

    L: List[str] = []
    L.append('"""由 AISBench 网关自动生成，请勿手工编辑。"""')
    L.append("from mmengine.config import read_base")
    L.append(f"from ais_bench.benchmark.models import {api['model_class']}")
    L.append("")

    summarizer_mod = SUMMARIZER_PERF if mode == "perf" else SUMMARIZER_ACCURACY
    L.append("with read_base():")
    L.append(f"    from {summarizer_mod} import summarizer")

    if is_gen:
        # 生成的数据集不 import 任何 datasets 模块 —— 它是 CustomDataset 的
        # 一个实例，配置直接以字面量写出来（见 _custom_dataset_config）。
        path = dataset_path or p.get("_gen_dataset_path")
        if not path and not validate_only:
            raise ValueError(
                "生成数据集的路径缺失：该数据集要先由网关生成才能提交，"
                "这通常是任务参数里的生成配置不完整导致的。"
            )
        abbr = p.get("_gen_abbr") or PREFIX_GEN_FAMILY
        L.append("")
        L.append(f"datasets = [{_py(_custom_dataset_config(str(path or '<待生成>/data.jsonl'), abbr))}]")
    else:
        L.append(
            "    from ais_bench.benchmark.configs.datasets."
            f"{family}.{variant} import {family}_datasets as _ds"
        )
        L.append("")
        L.append("datasets = [*_ds]")
        L.append("")

        if family == "synthetic":
            # 随机数据集的样本量由 RequestCount 控制，不走 --num-prompts
            syn = _synthetic_override(p)
            L.append(f"datasets[0]['config'] = {_py(syn)}")

    L.append("")
    L.append(f"models = [{_py(model_cfg)}]")
    L.append("")

    # 刻意**不**生成 infer / eval 块，交给 CLI 自己补。
    # cli/workers.py::Infer.update_cfg 会看 models[0]['attr']：
    #   attr == 'service' → OpenICLApiInferTask（走 HTTP）
    #   否则             → OpenICLInferTask（走本地推理）
    # 自己硬写 infer 很容易选错任务类，症状是
    #   TypeError: BaseAPIModel.generate() missing 1 required positional argument: 'output'
    # CLI 还会顺带设好 runner.max_num_workers、partitioner.out_dir 等，
    # 自己写反而容易和框架打架。

    data_dir = p.get("_data_dir", "/work").rstrip("/")
    work_dir = f"{data_dir}/outputs/{job['id']}"
    if stage != "main":
        work_dir = f"{work_dir}/{stage}"
    L.append(f"work_dir = {json.dumps(work_dir)}")
    L.append("")
    return "\n".join(L)


def write_config(
    s: Settings,
    job: Dict[str, Any],
    dataset_path: str | None = None,
    stage: str = "main",
) -> Path:
    d = s.configs_dir / job["id"]
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{stage}.py"
    path.write_text(
        build_config_source(job, dataset_path=dataset_path, stage=stage),
        encoding="utf-8",
    )
    return path


def build_argv(
    s: Settings,
    job: Dict[str, Any],
    stage: str = "main",
) -> List[str]:
    """构造容器内要执行的完整命令（含 ais_bench，不含 docker exec 前缀）。"""
    p = job.get("params") or {}
    mode = job.get("mode", "accuracy")
    cfg = str(s.configs_dir / job["id"] / f"{stage}.py")

    argv = ["ais_bench", cfg, "-m", "perf" if mode == "perf" else "all"]

    # synthetic 的样本量由 synthetic_config 的 RequestCount 控制。
    # 注意 --num-prompts 与配置里的 reader_cfg['test_range'] 互斥：
    # 若 test_range 已存在，--num-prompts 会被静默忽略（cli/utils.py::fill_test_range_use_num_prompts），
    # 所以这里只用 CLI 一种机制，不要两处都设。
    #
    # 精度模式也允许限制样本量：全量跑一次 mmlu 要几千条请求，
    # 调通路或做冒烟时按需截断更实际（跑出来的分数只代表这个子集）。
    #
    # 生成的数据集同理不传：它的条数由「数据条数」决定，数据集里就那么多行，
    # 再叠一个 --num-prompts 只会让两个数字打架。
    n = int(p.get("num_prompts") or 0)
    if n > 0 and p.get("dataset") not in ("synthetic", PREFIX_GEN_FAMILY):
        argv += ["--num-prompts", str(n)]

    warmups = p.get("num_warmups")
    if warmups is not None and str(warmups) != "":
        argv += ["--num-warmups", str(int(warmups))]

    if p.get("merge_ds"):
        argv.append("--merge-ds")
    return argv
