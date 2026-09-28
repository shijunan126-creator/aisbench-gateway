"""解析 aisbench 产物，归一化成前端能直接画图的形状。

产物布局（实测于 v3.1-20260630）：

  性能模式 <work>/<ts>/
    performances/<model_abbr>/<dataset>.json     端到端指标（吞吐/并发/请求数）
    performances/<model_abbr>/<dataset>.csv      逐请求分位指标（E2EL/TTFT/TPOT/...）
    performances/<model_abbr>/<dataset>_plot.html    aisbench 自带的并发/时间线图
    performances/<model_abbr>/<dataset>_details.jsonl 逐请求明细

  精度模式 <work>/<ts>/
    results/<model_abbr>/<dataset>.json          各数据集得分
    summary/summary_<ts>.csv                    汇总表（dataset,version,metric,mode,...）
    predictions/<model_abbr>/<dataset>.jsonl    逐条预测

两个必须注意的坑：
1. **带单位的指标是字符串**，如 "404.8321 ms"、"9.8806 req/s"、"209.9636 token/s"
   （见 calculators/base_perf_metric_calculator.py::_add_units_to_*），解析时要剥掉单位。
2. JSON 里的键是 **Total Generated Tokens**，不是 Total Output Tokens
   —— 单位映射表里写的是后者，已经过时，那个键根本不会出现。
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Dict, List

# 精度指标的取值白名单，取自 summarizers/default.py::METRIC_WHITELIST
ACCURACY_METRICS = [
    "score", "auc_score", "accuracy", "humaneval_pass@1", "rouge1",
    "avg_toxicity_score", "bleurt_diff", "matthews_correlation",
    "truth", "f1", "exact_match", "extract_rate",
]

# 性能 JSON 里的键 → 前端用的扁平键名
COMMON_KEYS = {
    "Benchmark Duration": "duration_ms",
    "Total Requests": "total_requests",
    "Failed Requests": "failed_requests",
    "Success Requests": "success_requests",
    "Concurrency": "concurrency",
    "Max Concurrency": "max_concurrency",
    "Request Throughput": "request_throughput",
    "Total Input Tokens": "total_input_tokens",
    "Prefill Token Throughput": "prefill_token_throughput",
    "Total Generated Tokens": "total_output_tokens",
    "Input Token Throughput": "input_token_throughput",
    "Output Token Throughput": "output_token_throughput",
    "Total Token Throughput": "total_token_throughput",
}

# 逐请求指标 → 扁平键前缀
PER_REQUEST_KEYS = {
    "E2EL": "e2el",
    "TTFT": "ttft",
    "TPOT": "tpot",
    "ITL": "itl",
    "InputTokens": "input_tokens",
    "OutputTokens": "output_tokens",
    "OutputTokenThroughput": "output_token_speed",
}

STATS = ["Average", "Min", "Max", "Median", "P75", "P90", "P99"]

_NUM_RE = re.compile(r"^\s*(-?[\d.]+(?:[eE][-+]?\d+)?)")


def to_number(v: Any) -> float | None:
    """把 "404.83 ms" / 4 / "1.2e3 token/s" 之类统一成 float。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _NUM_RE.match(str(v))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _metric_value(d: Any) -> Any:
    """端到端指标的值形如 {"total": <标量>}，取出那个标量。

    注意这里的值**本身是标量**（数字或带单位的字符串），不是嵌套字典 ——
    早期版本把它当字典处理，导致所有端到端指标静默丢失。
    """
    if isinstance(d, dict):
        if not d:
            return None
        # 单 stage（DefaultPerfMetricCalculator 恒为 "total"）取唯一值；
        # 多 stage 时优先 total
        if "total" in d:
            return d["total"]
        return next(iter(d.values()))
    return d


def parse_perf_json(path: Path) -> Dict[str, Any]:
    """端到端（common）指标。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    out: Dict[str, Any] = {}
    for raw_key, flat_key in COMMON_KEYS.items():
        if raw_key not in data:
            continue
        num = to_number(_metric_value(data[raw_key]))
        if num is not None:
            out[flat_key] = num
    return out


def parse_perf_csv(path: Path) -> Dict[str, Any]:
    """逐请求分位指标。"""
    out: Dict[str, Any] = {}
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("Performance Parameters") or "").strip()
            prefix = PER_REQUEST_KEYS.get(name)
            if not prefix:
                continue
            for stat in STATS:
                num = to_number(row.get(stat))
                if num is not None:
                    out[f"{prefix}_{stat.lower()}"] = num
            n = to_number(row.get("N"))
            if n is not None:
                out[f"{prefix}_n"] = int(n)
    return out


def find_runs(work_dir: Path) -> List[Path]:
    """aisbench 会在 work_dir 下再建一个时间戳目录。"""
    if not work_dir.exists():
        return []
    subs = [d for d in work_dir.iterdir() if d.is_dir() and re.fullmatch(r"\d{8}_\d{6}", d.name)]
    if not subs:
        # 有些情况下产物直接落在 work_dir
        return [work_dir] if (work_dir / "performances").exists() or (work_dir / "results").exists() else []
    return sorted(subs, key=lambda d: d.name)


def artifact_rel(path: Path, base: Path) -> str | None:
    """把产物路径转成相对 **产物根目录**（outputs/）的路径。

    产物接口（main.py::_serve_artifact）就是拿 `settings.outputs_dir` 当 base
    解析的，所以这里必须用同一个基准。

    **不能拿 `run_dir.parent` 当基准**：run_dir 是 `outputs/<job>/<时间戳>`，
    上一级只有 `<job>`，算出来的路径少了 `<时间戳>` 前面的那层 `<job>/`，
    前端拼出来的链接必然 404。这个 bug 让详情页的「原生可视化」三个按钮
    （时间线图 / RPS 分布 / 逐请求明细）一直点不开。

    转不出相对路径（产物不在 outputs 下）就返回 None，由调用方跳过 ——
    给一个点不开的链接比不给更误导。
    """
    try:
        return str(path.relative_to(base))
    except ValueError:
        return None


def collect_perf(run_dir: Path, artifacts_base: Path | None = None) -> List[Dict[str, Any]]:
    """收集一次性能运行的全部 (模型, 数据集) 结果。"""
    out: List[Dict[str, Any]] = []
    perf_root = run_dir / "performances"
    if not perf_root.exists():
        return out

    for model_dir in sorted(p for p in perf_root.iterdir() if p.is_dir()):
        for js in sorted(model_dir.glob("*.json")):
            dataset = js.stem
            if dataset == "tmp":
                continue
            rec: Dict[str, Any] = {
                "kind": "perf",
                "model_abbr": model_dir.name,
                "dataset": dataset,
                "metrics": {},
                "artifacts": {},
            }
            try:
                rec["metrics"].update(parse_perf_json(js))
            except Exception as e:  # noqa: BLE001
                rec["error"] = f"解析 {js.name} 失败: {e}"

            csv_path = model_dir / f"{dataset}.csv"
            if csv_path.exists():
                try:
                    rec["metrics"].update(parse_perf_csv(csv_path))
                except Exception as e:  # noqa: BLE001
                    rec["error"] = f"解析 {csv_path.name} 失败: {e}"

            base = artifacts_base or run_dir.parent
            for key, name in (("plot", f"{dataset}_plot.html"),
                              ("rps_plot", f"{dataset}_rps_distribution_plot.html"),
                              ("details", f"{dataset}_details.jsonl")):
                p = model_dir / name
                if not p.exists():
                    continue
                rel = artifact_rel(p, base)
                if rel:
                    rec["artifacts"][key] = rel

            # 把该数据集 ID 塞进去，便于多结果对比时区分
            rec["metrics"]["_concurrency"] = rec["metrics"].get("max_concurrency")
            out.append(rec)
    return out


def collect_accuracy(run_dir: Path) -> List[Dict[str, Any]]:
    """收集一次精度运行的全部数据集得分。"""
    out: List[Dict[str, Any]] = []
    res_root = run_dir / "results"
    if not res_root.exists():
        return out

    for model_dir in sorted(p for p in res_root.iterdir() if p.is_dir()):
        for js in sorted(model_dir.glob("*.json")):
            rec: Dict[str, Any] = {
                "kind": "accuracy",
                "model_abbr": model_dir.name,
                "dataset": js.stem,
                "metrics": {},
                "raw": {},
                "artifacts": {},
            }
            try:
                data = json.loads(js.read_text(encoding="utf-8"))
                for k, v in data.items():
                    if k == "details":
                        continue
                    num = to_number(v)
                    if num is not None and (
                        k in ACCURACY_METRICS or k.startswith("model_postprocess_")
                    ):
                        rec["metrics"][k] = num
                        rec["raw"][k] = v
                    elif isinstance(v, (str, int, float, bool)):
                        rec["raw"][k] = v
                # 主指标作为统一字段，方便跨数据集对比
                for cand in ACCURACY_METRICS:
                    if cand in rec["metrics"]:
                        rec["metrics"]["main_score"] = rec["metrics"][cand]
                        rec["main_metric"] = cand
                        break
            except Exception as e:  # noqa: BLE001
                rec["error"] = f"解析 {js.name} 失败: {e}"
            out.append(rec)
    return out


def parse_summary_csv(run_dir: Path) -> List[Dict[str, Any]]:
    """解析 summary/summary_<ts>.csv，得到跨数据集的汇总表。

    表头形如：dataset,version,metric,mode,<model_abbr...>
    单元格可能是 "87.50 (35/40)" 这种带正确数/总数的形式。
    """
    summ = run_dir / "summary"
    if not summ.exists():
        return []
    files = sorted(summ.glob("summary_*.csv"))
    if not files:
        return []
    rows: List[Dict[str, Any]] = []
    with files[-1].open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            item: Dict[str, Any] = {
                "dataset": row.get("dataset", ""),
                "metric": row.get("metric", ""),
                "mode": row.get("mode", ""),
                "scores": {},
            }
            for k, v in row.items():
                if k in ("dataset", "version", "metric", "mode", "total_count", None):
                    continue
                num = to_number(v)
                if num is not None:
                    item["scores"][k] = num
            rows.append(item)
    return rows


def collect(run_dir: Path, mode: str,
            artifacts_base: Path | None = None) -> List[Dict[str, Any]]:
    if mode == "perf":
        return collect_perf(run_dir, artifacts_base)
    if mode == "accuracy":
        return collect_accuracy(run_dir)
    recs = collect_accuracy(run_dir)
    return recs or collect_perf(run_dir, artifacts_base)


_ERR_LINE_RE = re.compile(
    r"^(?:[A-Za-z_.]*(?:Error|Exception|AssertionError)\b.*|[A-Za-z_.]+Error:.*)$",
    re.MULTILINE,
)


def find_failure_reason(run_dir: Path) -> str | None:
    """从产物目录的日志里捞出真正的失败原因。

    必要性：aisbench 报出来的东西常常跟真实原因八竿子打不着 ——

    - **模型服务连不上**时，它在日志里打了一张很清楚的「失败原因汇总」表
      （`| ... ClientConnectorError: Cannot connect to host ... | 6 |`），
      但随后抛的异常却是 `different structure of perf data`。
    - **评估步骤失败时进程退出码仍然是 0**。比如 HumanEval 被 --num-prompts
      截断后会抛 `AssertionError: Some problems are not attempted.`，
      但 aisbench 照样 exit 0，任务被标成"成功"却没有任何结果。

    两种情况都靠这个函数把真实原因挖出来给用户看。优先取那张汇总表
    （信息量最大），没有就退回到最后一条异常行。
    """
    if not run_dir or not run_dir.exists():
        return None

    logs_dir = run_dir / "logs"
    logs = sorted(logs_dir.rglob("*.out")) if logs_dir.exists() else []
    if not logs:
        return None

    texts = []
    for lp in logs:
        try:
            texts.append((lp.name, lp.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue

    # 1) 失败原因汇总表 —— 最贴近根因
    for _name, text in texts:
        summary = _parse_failed_summary(text)
        if summary:
            return summary

    # 2) 退回最后一条异常行
    for name, text in texts:
        if "Traceback" not in text and "Error" not in text:
            continue
        hits = _ERR_LINE_RE.findall(text)
        if hits:
            return f"{name}: {hits[-1].strip()[:300]}"
    return None


_FAILED_SUMMARY_RE = re.compile(r"failed reasons summary:(.*?)(?:\n\s*\n|\Z)", re.DOTALL)


def _parse_failed_summary(text: str) -> str | None:
    """从日志文本里解析 aisbench 的「失败原因汇总」表。

    原始长这样（外面还套着 rich 的表格边框）：

        Task finished, failed reasons summary:
        | Failed Reason                                       | Count |
        | After 2 retries, request failed with exception: ... |   6   |

    只保留真正的数据行（最后一格是数字的那些），表头和边框都丢掉。
    """
    m = _FAILED_SUMMARY_RE.search(text)
    if not m:
        return None

    out = []
    for raw in m.group(1).splitlines():
        ln = raw.strip()
        if not ln.startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        cells = [c for c in cells if c]
        # 数据行的特征：最后一格是纯数字（出现次数）
        if len(cells) >= 2 and cells[-1].isdigit():
            reason = " | ".join(cells[:-1])
            out.append(f"{reason} (×{cells[-1]})")
        if len(out) >= 3:
            break
    return "；".join(out)[:400] if out else None


def find_log_failure_reason(log_path: Path) -> str | None:
    """从任务日志里挖出 aisbench 的「失败原因汇总」。

    `find_failure_reason` 是靠产物目录找的，拿不到时（配置没生成出来、
    进程根本没启动）就轮到它。失败原因总在日志末尾，所以只读末尾一段
    ——日志在长压测下能有几百 MB，整读会把容器内存吃掉。
    """
    try:
        text = read_log_tail(Path(log_path))
    except OSError:
        return None
    return _parse_failed_summary(text) if text else None


def read_log_tail(path: Path, max_bytes: int = 200_000) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        return f.read().decode("utf-8", errors="replace")
