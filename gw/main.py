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
        "tokenizer_dirs": tokenizer_candidates(),
        "selfcheck": runtime.selfcheck(settings),
        "container_name": settings.container,
        "models_mount": str(settings.models_dir),
    })


def tokenizer_candidates() -> List[str]:
    """可选 tokenizer 路径 = config 里配的 + data/models 下扫到的（容器内路径）。"""
    out: List[str] = list(settings.tokenizer_dirs)
    md = settings.models_dir
    if md.exists():
        for p in sorted(md.iterdir()):
            if p.is_dir():
                out.append(f"{settings.models_dir}/{p.name}")
    return out


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
    """增量拉取日志；offset 是已读字节数。"""
    jid = m.group("jid")
    job = store.get_job(jid)
    if not job:
        raise ApiError(404, "任务不存在")
    # 负数也要挡住：文件对象的 seek(-1) 会抛 OSError 变成 500
    offset = max(0, _int_arg(q, "offset", 0))

    path = settings.outputs_dir / jid / "run.log"
    if not path.exists():
        return 200, "application/json; charset=utf-8", json_bytes(
            {"text": "", "offset": 0, "status": job["status"]})

    size = path.stat().st_size
    if offset > size:  # 文件被截断/重建
        offset = 0
    with path.open("rb") as f:
        f.seek(offset)
        # 单次最多回这么多：首次打开（offset=0）时整个 run.log 可能有几百 MB，
        # 整读会把容器内存吃掉，而日志是增量轮询的，剩下的下次再取。
        data = f.read(_LOG_CHUNK)
    return 200, "application/json; charset=utf-8", json_bytes({
        "text": data.decode("utf-8", errors="replace"),
        "offset": offset + len(data),
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

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
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
                    self._send(status, ctype, payload)
                    return

            if method == "GET":
                if path.startswith("/api/artifacts/"):
                    self._serve_artifact(path[len("/api/artifacts/"):])
                    return
                if not path.startswith("/api/"):
                    self._serve_static(path)
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

    # ---- 静态文件 ----
    def _serve_static(self, path: str) -> None:
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
        try:
            data = target.read_bytes()
        except OSError as e:
            self._send(*err(500, f"读取文件失败: {e}"))
            return
        self._send(200, ctype, data)

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
        try:
            data = target.read_bytes()
        except OSError as e:
            self._send(*err(500, f"读取文件失败: {e}"))
            return
        self._send(200, ctype, data)


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
