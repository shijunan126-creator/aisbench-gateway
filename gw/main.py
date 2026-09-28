"""AISBench 网关 HTTP 服务（仅用标准库 http.server）。

**为什么不用 FastAPI**：交付包要在客户机上「解压即用」。FastAPI 依赖
pydantic，而 pydantic 的核心 `pydantic_core` 在 PyPI 上是平台相关的 wheel
（`...manylinux...x86_64.whl`），客户机架构或 Python 版本对不上就装不上，
离线 pip 安装会直接失败。用标准库后整个网关零第三方依赖，
只需要客户机有 python3（>=3.8）即可。

路由用一张 (method, 正则, 处理函数) 表，处理函数返回 (状态码, 内容类型, 字节)。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import signal
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from . import catalog, confgen, dataset_gen, datasets_mgr, runtime
from .runner import Runner
from .settings import ROOT, settings
from .store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("gw")

STATIC_DIR = ROOT / "static"

store = Store(settings)
runner = Runner(settings, store)

Response = Tuple[int, str, bytes]


# ------------------------------------------------------------------ 工具
def json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def err(status: int, msg: str) -> Response:
    return status, "application/json; charset=utf-8", json_bytes({"detail": msg})


class ApiError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status
        self.msg = msg


# ------------------------------------------------------------------ 处理器
def h_config(_m, _q, _b) -> Response:
    return 200, "application/json; charset=utf-8", json_bytes({
        "api_types": catalog.API_TYPES,
        "defaults": settings.defaults,
        "tokenizer_dirs": settings.tokenizer_dirs,
        "tokenizer_candidates": tokenizer_candidates(),
        "selfcheck": runtime.selfcheck(settings),
        "container_name": settings.container,
        "models_mount": str(settings.models_dir),
    })


# 常见 tokenizer/模型目录的特征文件。HuggingFace/ModelScope 下载的模型目录
# 都会带其中若干个，据此把「装了模型的文件夹」从一堆目录里认出来。
_TOKENIZER_MARKER_FILES = (
    "tokenizer.json", "tokenizer_config.json", "vocab.json", "vocab.txt",
    "merges.txt", "spiece.model", "sentencepiece.bpe.model", "tokenizer.model",
)


def _looks_like_model_dir(p: Path) -> bool:
    return p.is_dir() and any((p / f).is_file() for f in _TOKENIZER_MARKER_FILES)


def tokenizer_candidates() -> List[Dict[str, str]]:
    """扫描可用的模型/tokenizer 目录，供页面下拉选择。

    config.ini 的 aisbench.tokenizer_dirs 里可以填两种目录：
      - 模型目录本身（含 tokenizer.json 等特征文件）
      - 装了多个模型的**父目录**（如 /data/models）——扫描其下一层子目录
    加上数据目录自带的 data/models/<名字>。

    返回 [{name, path}, ...]：name 是模型目录名（页面上填/选它），
    path 是容器内完整路径（提交时由 resolve_tokenizer_path 拼回去）。
    """
    out: Dict[str, str] = {}

    def add(path: Path) -> None:
        if _looks_like_model_dir(path):
            out.setdefault(path.name, str(path))

    bases = list(settings.tokenizer_dirs) + [str(settings.models_dir)]
    for raw in bases:
        base = Path(raw)
        if not base.is_dir():
            continue
        add(base)
        for sub in sorted(base.iterdir()):
            if sub.name.startswith(".") or not sub.is_dir():
                continue
            add(sub)
    return [{"name": n, "path": p} for n, p in sorted(out.items())]


def resolve_tokenizer_path(raw: str) -> str:
    """把页面填的 tokenizer 路径解析成容器内完整路径。

    允许三种写法（对应不同习惯）：
      - 完整容器内路径：存在即原样接受
      - 模型目录名：如 Qwen3.5-35B-A3B，在 tokenizer_dirs / data/models 下找同名目录
      - 相对路径：如 models/Qwen3 或 …/tokenizer.json（填到文件上取其目录）

    找不到时抛 400，并把当前可选的模型名列出来，省得用户猜。
    """
    p = (raw or "").strip()
    if not p:
        return p
    if Path(p).exists():
        return p
    cands = tokenizer_candidates()
    by_name = {c["name"]: c["path"] for c in cands}
    if p in by_name:
        return by_name[p]
    for base in list(settings.tokenizer_dirs) + [str(settings.models_dir)]:
        cand = Path(base) / p.lstrip("/")
        if cand.exists():
            if cand.is_file():
                cand = cand.parent
            return str(cand)
    # 兜底：用户把完整路径填成了别的容器外形态（比如宿主机路径），
    # 取最后一段当目录名再试一次
    tail_name = p.rstrip("/").rsplit("/", 1)[-1]
    if tail_name in by_name:
        return by_name[tail_name]
    names = "\n  - ".join(sorted(by_name)) if by_name else "（无）"
    raise ApiError(
        400,
        f"找不到模型/tokenizer 目录「{p}」。\n"
        f"当前扫描到的模型目录：\n  - {names}\n"
        "请从上面选一个，或在 config.ini 的 aisbench.tokenizer_dirs 里"
        "配置模型所在目录后重启网关。",
    )


def h_selfcheck(_m, _q, _b) -> Response:
    """重新做一次运行环境自检。

    以前这里能重建容器，现在网关自己就住在容器里，重建等于自杀，
    所以只能报告状态；要重建请执行 ./start.sh。
    """
    return 200, "application/json; charset=utf-8", json_bytes(
        runtime.selfcheck(settings))


def h_datasets(_m, _q, _b) -> Response:
    return 200, "application/json; charset=utf-8", json_bytes(
        datasets_mgr.status(settings, store))


def h_dataset_download(m, _q, _b) -> Response:
    family = m.group("family")
    ds = catalog.get_dataset(family)
    if not ds:
        raise ApiError(404, f"未知数据集 {family}")
    if ds.get("builtin"):
        return 200, "application/json; charset=utf-8", json_bytes(
            {"started": False, "message": "内置数据集，无需下载"})
    if not ds.get("url"):
        raise ApiError(400, "该数据集没有可用的下载地址")
    started = datasets_mgr.start_download(settings, store, family)
    return 200, "application/json; charset=utf-8", json_bytes(
        {"started": started, "message": "已开始下载" if started else "正在下载中"})


def h_jobs_submit(_m, _q, body) -> Response:
    body = body or {}
    params = body.get("params") or {}
    mode = body.get("mode") or "perf"

    family = params.get("dataset")
    ds = catalog.get_dataset(family)
    if not ds:
        raise ApiError(400, f"未知数据集: {family}")
    if not (params.get("base_url") or "").strip():
        raise ApiError(400, "base_url 不能为空")
    if mode == "accuracy" and family in catalog.PERF_ONLY_FAMILIES:
        raise ApiError(
            400, f"{ds['label']} 只支持性能模式（aisbench 的 ONLY_PERF_DATASETS 限制）")
    if mode == "accuracy" and family == catalog.PREFIX_GEN_FAMILY:
        raise ApiError(
            400,
            f"{ds['label']} 只支持性能模式：合成出来的 answer 只是占位符，"
            "精度得分没有意义。",
        )

    # 提前跑一遍生成逻辑，把配置错误（缺 tokenizer 等）在提交时就反馈给用户。
    # validate_only：合成数据集这时还没生成，路径不存在，校验只关心
    # 接口类型/base_url/tokenizer 这些参数，不需要真文件。
    try:
        confgen.build_config_source(
            {"id": "__validate__", "mode": mode, "params": params}, validate_only=True)
        if family == catalog.PREFIX_GEN_FAMILY:
            # 生成参数本身也要校验：长度放不下、前缀个数超过语料这类问题
            # 等到执行时才报就太晚了
            dataset_gen.spec_from_params(params)
    except (ValueError, dataset_gen.GenError) as e:
        raise ApiError(400, str(e)) from e

    # 页面里可以只填模型目录名（如 Qwen3.5-35B-A3B），这里解析成容器内完整
    # 路径再入库——后续 confgen 写配置、数据集生成都直接用，不用再各处兜底。
    # 提前到提交时解析：填错立刻反馈，而不是等 aisbench 跑起来才炸。
    if params.get("tokenizer_path"):
        params["tokenizer_path"] = resolve_tokenizer_path(params["tokenizer_path"])
    _check_tokenizer(params)

    label = body.get("label") or f"{ds['label']} · {'精度' if mode == 'accuracy' else '性能'}"
    note = (body.get("note") or "").strip()
    params["_data_dir"] = settings.data_path
    try:
        res = runner.submit(params, mode, note=note)
    except ValueError as e:
        raise ApiError(400, str(e)) from e
    if isinstance(res, dict):
        return 200, "application/json; charset=utf-8", json_bytes(res)
    store.update_job(res, label=label)
    return 200, "application/json; charset=utf-8", json_bytes(
        {"job_ids": [res], "parent_id": None})


def h_job_note(m, _q, body) -> Response:
    """给任务写备注。备注会盖过自动生成的标签，出现在列表和对比图里 ——
    对比多组参数时，"这条是 75% 那组"得一眼看得出来。"""
    jid = m.group("jid")
    if not store.get_job(jid):
        raise ApiError(404, "任务不存在")
    note = ((body or {}).get("note") or "").strip()
    if len(note) > 200:
        raise ApiError(400, "备注最多 200 字")
    store.set_note(jid, note)
    return 200, "application/json; charset=utf-8", json_bytes({"ok": True, "note": note})


def _check_tokenizer(params: Dict[str, Any]) -> None:
    """提交前确认 tokenizer 路径在容器内可见。

    不查的话要等 aisbench 跑到一半才报
    `synthetic dataset miss required param`，用户很难定位。
    """
    if params.get("dataset") not in ("synthetic", "sharegpt", catalog.PREFIX_GEN_FAMILY):
        return
    p = (params.get("tokenizer_path") or "").strip()
    if not p:
        return  # confgen 会给出"必填"的提示
    # 网关就在容器里，直接看文件系统即可，不用再 docker exec
    if not Path(p).exists():
        raise ApiError(
            400,
            f"tokenizer 路径不存在：{p}\n"
            f"请把它放进数据目录的 models/ 下（表单里填 {settings.models_dir}/<名字>），"
            f"或在 config.ini 的 aisbench.tokenizer_dirs 里配置宿主机模型目录后重启。",
        )


def h_jobs_list(_m, q, _b) -> Response:
    limit = _int_arg(q, "limit", 200)
    jobs = store.list_jobs(limit=limit)
    for j in jobs:
        if j["kind"] == "sweep":
            j["children"] = store.children(j["id"])
    # 扫描的备注写在父任务上，子任务在这里补出来
    store.resolve_notes(jobs)
    return 200, "application/json; charset=utf-8", json_bytes(jobs)


def h_job_detail(m, _q, _b) -> Response:
    job = store.get_job(m.group("jid"))
    if not job:
        raise ApiError(404, "任务不存在")
    job["results"] = store.results_for_job(job["id"])
    if job["kind"] == "sweep":
        job["children"] = store.children(job["id"])
        job["series"] = runner.sweep_series(job["id"])
    return 200, "application/json; charset=utf-8", json_bytes(job)


def h_job_cancel(m, _q, _b) -> Response:
    jid = m.group("jid")
    if not store.get_job(jid):
        raise ApiError(404, "任务不存在")
    return 200, "application/json; charset=utf-8", json_bytes(
        {"cancelled": runner.cancel(jid)})


def h_job_delete(m, _q, _b) -> Response:
    jid = m.group("jid")
    if not store.get_job(jid):
        raise ApiError(404, "任务不存在")
    runner.delete(jid)
    return 200, "application/json; charset=utf-8", json_bytes({"deleted": True})


def h_job_reparse(m, _q, _b) -> Response:
    """按当前解析逻辑重新读取磁盘产物，刷新入库指标（不必重跑压测）。"""
    from . import results as results_mod

    jid = m.group("jid")
    job = store.get_job(jid)
    if not job:
        raise ApiError(404, "任务不存在")

    run_dir = job.get("run_dir")
    if not run_dir:
        runs = results_mod.find_runs(settings.outputs_dir / jid)
        run_dir = str(runs[-1]) if runs else None
    if not run_dir or not Path(run_dir).exists():
        raise ApiError(400, "找不到产物目录，无法重新解析")

    recs = results_mod.collect(Path(run_dir), job["mode"], settings.outputs_dir)
    n = store.replace_results(jid, recs)
    return 200, "application/json; charset=utf-8", json_bytes(
        {"reparsed": n, "run_dir": run_dir})


def h_job_log(m, q, _b) -> Response:
    """增量拉取日志；offset 是已读字节数。

    `tail` 参数（字节）只在首次打开时用：直接从文件**末尾**往前取这么多开始读，
    跳过的部分由返回值 `skipped` 告知。长压测的 run.log 能有几百 MB，
    而用户关心的是最新的输出（以及失败时网关抽取到错误卡片里的原因），
    从头逐块下载只是浪费。
    """
    jid = m.group("jid")
    job = store.get_job(jid)
    if not job:
        raise ApiError(404, "任务不存在")
    # 负数也要挡住：文件对象的 seek(-1) 会抛 OSError 变成 500
    offset = max(0, _int_arg(q, "offset", 0))
    tail = max(0, _int_arg(q, "tail", 0))

    path = settings.outputs_dir / jid / "run.log"
    if not path.exists():
        return 200, "application/json; charset=utf-8", json_bytes(
            {"text": "", "offset": 0, "size": 0, "skipped": 0, "status": job["status"]})

    size = path.stat().st_size
    if offset > size:  # 文件被截断/重建
        offset = 0

    start = offset
    skipped = 0
    want = _LOG_CHUNK
    if tail and start == 0 and size > tail:
        start = size - tail
        # 对齐到下一个行首：从任意字节处开读会把首行劈成两半。
        # 日志是行式的，跳到下一个换行即可；探测窗口给 4KB，兼容长行。
        with path.open("rb") as f:
            f.seek(start)
            probe = f.read(4096)
        nl = probe.find(b"\n")
        if nl >= 0:
            start += nl + 1
        skipped = start
        # tail 请求一次给全（上限 MAX_LOG_TAIL）：已结束的任务前端只拉这一次，
        # 若还按 256KB 分块，用户看到的会是尾部区域**最老**的一段而不是结果输出。
        # 增量轮询（offset>0）不受影响，仍按 _LOG_CHUNK 分块。
        want = min(tail, _MAX_LOG_TAIL)

    with path.open("rb") as f:
        f.seek(start)
        # 常规增量单次最多 _LOG_CHUNK：首次打开（offset=0）时整个 run.log
        # 可能有几百 MB，整读会把容器内存吃掉，剩下的下次再取。
        data = f.read(want)
    # 按 UTF-8 字符边界截尾：定长读取可能正好切在多字节字符中间，
    # 直接 decode 会把边界字符变成乱码 �。只消费完整前缀（offset 也只前移到这），
    # 不完整的尾部留到下一次读取，最多回退 3 字节（UTF-8 序列最长 4 字节）。
    for back in range(4):
        try:
            text = data[: len(data) - back or None].decode("utf-8")
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
        back = 0
    return 200, "application/json; charset=utf-8", json_bytes({
        "text": text,
        "offset": start + len(data) - back,
        "size": size,
        "skipped": skipped,
        "status": job["status"],
    })


def h_results(_m, q, _b) -> Response:
    kind = (q.get("kind") or [None])[0]
    raw_ids = (q.get("job_ids") or [""])[0]
    ids = [i for i in raw_ids.split(",") if i] or None
    return 200, "application/json; charset=utf-8", json_bytes(
        store.list_results(kinds=[kind] if kind else None, job_ids=ids))


def h_results_compare(_m, q, _b) -> Response:
    raw = (q.get("job_ids") or [""])[0]
    ids = [i for i in raw.split(",") if i]
    if not ids:
        raise ApiError(400, "job_ids 不能为空")
    out = []
    for jid in ids:
        job = store.get_job(jid)
        if not job:
            continue
        p = job.get("params") or {}
        note = (job.get("note") or "").strip()
        parent = job.get("parent_id")
        if not note and parent:
            # 扫描的备注写在父任务上，子任务回退过去
            p = store.get_job(parent)
            note = ((p or {}).get("note") or "").strip()
        auto = job.get("label") or jid
        # 子任务的 auto 是「并发 N」。带上它，否则同一组扫描的几条在图上会同名
        # —— 而"几档并发之间怎么变"恰恰是扫描最要看的。
        label = f"{note} · {auto}" if (note and parent) else (note or auto)
        out.append({
            "job_id": jid,
            # label 优先用备注：对比页的每一张图和表都读这个字段，
            # 备注是用户自己写的"这条是什么"，比自动标签更有辨识度。
            # 自动标签另给一个字段，前端可以并排显示。
            "label": label,
            "note": note,
            "auto_label": auto,
            "mode": job["mode"],
            "dataset": p.get("dataset"),
            "concurrency": p.get("concurrency"),
            "api_type": p.get("api_type"),
            "model": p.get("model"),
            "created_at": job["created_at"],
            "results": store.results_for_job(jid),
        })
    return 200, "application/json; charset=utf-8", json_bytes({"runs": out})


def h_health(_m, _q, _b) -> Response:
    return 200, "application/json; charset=utf-8", json_bytes({"ok": True})


# ------------------------------------------------------------------ 路由表
_ARTIFACT_SEG = re.compile(r"^[A-Za-z0-9_./-]+$")

# 请求体上限。表单提交只有几 KB，留 1MB 绰绰有余
_MAX_BODY = 1 << 20

# 日志接口单次最多回 256KB：run.log 在长压测下可能几百 MB，
# 整读会把容器内存吃掉（还要同时供 aisbench 跑压测）。前端是增量轮询的，
# 剩下的下次再取。
_LOG_CHUNK = 256 * 1024

# tail 参数的上限（见 h_job_log）：太大等于把整读的内存问题又请回来
_MAX_LOG_TAIL = 8 * 1024 * 1024

ROUTES: List[Tuple[str, "re.Pattern[str]", Callable]] = [
    ("GET", re.compile(r"^/api/health$"), h_health),
    ("GET", re.compile(r"^/api/config$"), h_config),
    ("POST", re.compile(r"^/api/selfcheck$"), h_selfcheck),
    ("GET", re.compile(r"^/api/datasets$"), h_datasets),
    ("POST", re.compile(r"^/api/datasets/(?P<family>[A-Za-z0-9_]+)/download$"), h_dataset_download),
    ("POST", re.compile(r"^/api/jobs$"), h_jobs_submit),
    ("GET", re.compile(r"^/api/jobs$"), h_jobs_list),
    ("GET", re.compile(r"^/api/jobs/(?P<jid>[A-Za-z0-9]+)$"), h_job_detail),
    ("POST", re.compile(r"^/api/jobs/(?P<jid>[A-Za-z0-9]+)/cancel$"), h_job_cancel),
    ("POST", re.compile(r"^/api/jobs/(?P<jid>[A-Za-z0-9]+)/note$"), h_job_note),
    ("POST", re.compile(r"^/api/jobs/(?P<jid>[A-Za-z0-9]+)/reparse$"), h_job_reparse),
    ("DELETE", re.compile(r"^/api/jobs/(?P<jid>[A-Za-z0-9]+)$"), h_job_delete),
    ("GET", re.compile(r"^/api/jobs/(?P<jid>[A-Za-z0-9]+)/log$"), h_job_log),
    ("GET", re.compile(r"^/api/results$"), h_results),
    ("GET", re.compile(r"^/api/results/compare$"), h_results_compare),
]


def _int_arg(q: Dict[str, List[str]], key: str, default: int) -> int:
    try:
        return int((q.get(key) or [str(default)])[0])
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ 请求处理
class Handler(BaseHTTPRequestHandler):
    server_version = "AISBenchGateway"
    protocol_version = "HTTP/1.1"

    # 默认实现会把每个请求打到 stderr，太吵
    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path.startswith("/api/health"):
            return
        log.debug("%s %s", self.command, self.path)

    # ---- 入口 ----
    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        # 探活/下载工具常用 HEAD；无 body，其余语义同 GET
        self._handle("GET", head_only=True)

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _handle(self, method: str, head_only: bool = False) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        try:
            body = self._read_json() if method in ("POST", "PUT") else None

            for m_method, pattern, fn in ROUTES:
                if m_method != method:
                    continue
                m = pattern.match(path)
                if m:
                    status, ctype, payload = fn(m, query, body)
                    if head_only:
                        self._send_headers_only(status, ctype, len(payload))
                    else:
                        self._send(status, ctype, payload)
                    return

            if method == "GET":
                if path.startswith("/api/artifacts/"):
                    self._serve_artifact(path[len("/api/artifacts/"):])
                    return
                if not path.startswith("/api/"):
                    self._serve_static(path, head_only=head_only)
                    return

            self._send(*err(404, f"没有这个接口: {method} {path}"))

        except ApiError as e:
            self._send(*err(e.status, e.msg))
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            log.error("处理 %s %s 出错:\n%s", method, path, traceback.format_exc())
            self._send(*err(500, f"服务端错误: {e}"))

    def _read_json(self) -> Optional[Dict[str, Any]]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return None
        # **必须有上限**：表单提交只有几 KB，而这里是「声明多少就读多少」，
        # 客户端只要发一个 Content-Length: 4000000000 然后不发 body，
        # 这个线程就会一直阻塞并把收到的字节全缓冲进内存。网关监听 0.0.0.0
        # 且无鉴权，局域网里谁都能反复建连把内存和线程耗光。
        if n > _MAX_BODY:
            raise ApiError(413, f"请求体过大（{n} 字节，上限 {_MAX_BODY}）")
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ApiError(400, f"请求体不是合法 JSON: {e}") from e
        # 顶层必须是对象：`[1,2]` 或 `"x"` 这种会让下游的 body.get(...)
        # 抛 AttributeError 变成 500，本该是 400
        if body is not None and not isinstance(body, dict):
            raise ApiError(400, "请求体必须是一个 JSON 对象")
        return body

    def _send(self, status: int, ctype: str, payload: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_headers_only(self, status: int, ctype: str, length: int) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_file(self, status: int, ctype: str, path: Path,
                   cache: bool = False, head_only: bool = False) -> None:
        """流式发送文件。

        产物（逐请求明细 jsonl、run.log）在长压测下能有几百 MB，
        read_bytes 整读会把容器内存吃掉——网关和 aisbench 同住一个容器，
        内存被吃光就是压测现场直接挂。Content-Length 已知，按块搬运即可。
        """
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(path.stat().st_size))
            # vendor 库（plotly 4.8MB）内容只随代码更新，允许浏览器缓存；
            # 其余保持 no-store，保证改前端刷新即生效
            self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
            self.end_headers()
            if head_only:
                return
            with path.open("rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- 静态文件 ----
    def _serve_static(self, path: str, head_only: bool = False) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._send(*err(400, "非法路径"))
            return
        if not target.exists() or not target.is_file():
            self._send(*err(404, "文件不存在"))
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send_file(200, ctype, target, cache=rel.startswith("vendor/"),
                        head_only=head_only)

    # ---- 产物文件 ----
    def _serve_artifact(self, rel: str) -> None:
        if not rel or not _ARTIFACT_SEG.match(rel) or ".." in rel:
            self._send(*err(400, "非法路径"))
            return
        base = settings.outputs_dir.resolve()
        target = (base / rel).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            self._send(*err(400, "非法路径"))
            return
        if not target.exists() or not target.is_file():
            self._send(*err(404, "文件不存在"))
            return

        suffix = target.suffix.lower()
        if suffix == ".html":
            ctype = "text/html; charset=utf-8"
        elif suffix in (".json", ".jsonl", ".csv", ".txt", ".log", ".out"):
            ctype = "text/plain; charset=utf-8"
        else:
            ctype = "application/octet-stream"
        self._send_file(200, ctype, target)


# ------------------------------------------------------------------ 启动
def _install_signal_handlers(httpd: ThreadingHTTPServer) -> None:
    """让 SIGTERM/SIGINT 走优雅退出。

    **这一步不能省。** 网关是容器的主进程，也就是 PID 1。Linux 内核对 PID 1
    会把「默认动作是终止」的信号（含 SIGTERM）**直接忽略**，除非进程自己装了处理器。
    Python 默认不处理 SIGTERM，于是 `docker stop` 会干等 10 秒然后 SIGKILL 硬杀，
    正在跑的任务被硬中断，来不及收尾。

    装了处理器之后 `docker stop` 秒级完成。
    """
    def _handler(signum: int, _frame: Any) -> None:
        log.info("收到信号 %s，正在停止…", signum)
        # 在另一个线程里关 server，避免在信号处理器里做重活
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):  # 非主线程等场景
            pass


def main() -> int:
    # 环境自检。不可用就打印清楚的错误并退出，而不是带着病跑到一半才炸。
    try:
        runtime.require_ok(settings)
        info = runtime.selfcheck(settings)
        log.info(
            "运行环境就绪：ais_bench=%s，数据目录=%s，磁盘剩余 %s GB",
            info.get("ais_bench"), info.get("data_dir"), info.get("disk_free_gb"),
        )
    except RuntimeError as e:
        log.error("%s", e)
        return 2

    # 对账：上次进程退出时留下的"运行中"任务已经没人管了。它们启动的进程
    # 是上个容器进程的子进程，随容器重启一起消亡了，所以这里只需把状态改对。
    for j in store.running_jobs():
        log.warning("任务 %s 在上次退出时中断，标记为失败", j["id"])
        store.update_job(j["id"], status="failed", error="网关重启导致任务中断",
                         finished_at=time.time())

    runner.start()

    httpd = ThreadingHTTPServer((settings.host, settings.port), Handler)
    httpd.daemon_threads = True
    _install_signal_handlers(httpd)

    log.info("AISBench 网关已启动 → http://%s:%d", settings.host, settings.port)
    if settings.host == "0.0.0.0":
        log.info("（监听所有网卡，局域网内可用本机 IP 访问）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("收到中断信号，正在停止…")
    finally:
        runner.shutdown()
        httpd.server_close()
        log.info("已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
