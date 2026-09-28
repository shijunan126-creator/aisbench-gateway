"""数据集就绪检测与下载。

镜像里**只带 synthetic**，其余数据集都要下载（见各 family 的 README）。
数据根是容器的 AIS_BENCH_DATASETS_CACHE，网关设为 /work/datasets，
所以宿主机上就是 <data>/datasets。

配置里的 path='ais_bench/datasets/xxx' 会被拼到该根下
（benchmark/datasets/utils/datasets.py::get_data_path），
所以就绪判据 = <data>/datasets/<ready_path> 是否存在。
"""

from __future__ import annotations

import logging
import shutil
import tarfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List

from . import catalog
from .catalog import DATASETS
from .settings import Settings
from .store import Store

log = logging.getLogger("gw.datasets")

_download_lock = threading.Lock()
_active: Dict[str, threading.Thread] = {}


def is_ready(s: Settings, ds: Dict[str, Any]) -> bool:
    if ds.get("builtin"):
        return True
    rp = ds.get("ready_path") or ""
    if not rp:
        return False
    return (s.datasets_dir / rp).exists()


def status(s: Settings, store: Store) -> List[Dict[str, Any]]:
    dl = store.get_downloads()
    out = []
    for ds in DATASETS:
        d = dl.get(ds["family"], {})
        out.append({
            "family": ds["family"],
            "label": ds["label"],
            "note": ds.get("note", ""),
            "size_mb": ds.get("size_mb", 0),
            "builtin": bool(ds.get("builtin")),
            "perf_only": catalog.is_perf_only(ds["family"]),
            "url": ds.get("url"),
            "ready": is_ready(s, ds),
            "variants": ds["variants"],
            "default_variant": ds["default_variant"],
            "download": {
                "status": d.get("status", "idle"),
                "progress": d.get("progress", 0) or 0,
                "message": d.get("message", ""),
            },
        })
    return out


def _download_to(url: str, dest: Path, on_progress) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "aisbench-gateway/1.0"})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=120) as r:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        last = 0.0
        with tmp.open("wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if on_progress and now - last > 0.4:
                    last = now
                    on_progress(done, total)
    tmp.replace(dest)


def _extract(archive: Path, into: Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            z.extractall(into)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as t:
            t.extractall(into)
    else:
        raise ValueError(f"无法识别的压缩包格式: {archive.name}")


def _run_download(s: Settings, store: Store, family: str) -> None:
    ds = next((d for d in DATASETS if d["family"] == family), None)
    if not ds or not ds.get("url"):
        store.set_download(family, status="error", message="该数据集没有可用的下载地址",
                           finished_at=time.time())
        return

    tmpdir = s.datasets_dir / ".tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    url = ds["url"]
    name = url.split("/")[-1].split("?")[0]
    archive = tmpdir / name

    def on_progress(done: int, total: int) -> None:
        pct = (done / total * 100) if total else 0
        mb = done / 1e6
        msg = f"已下载 {mb:.1f} MB" + (f" / {total / 1e6:.1f} MB" if total else "")
        store.set_download(family, status="downloading", progress=round(pct, 1), message=msg)

    try:
        store.set_download(family, status="downloading", progress=0,
                           message="开始下载", started_at=time.time(), finished_at=None)
        _download_to(url, archive, on_progress)

        store.set_download(family, status="extracting", progress=99, message="解压中")
        target = s.datasets_dir / (ds.get("extract_into") or "")

        if ds.get("filename"):
            # 单文件数据集（如 sharegpt 的 json），直接放到目标目录下
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(archive), str(target / ds["filename"]))
        else:
            _extract(archive, target)
            archive.unlink(missing_ok=True)

        ok = is_ready(s, ds)
        store.set_download(
            family,
            status="ready" if ok else "error",
            progress=100,
            message="下载完成" if ok else f"解压完成但未找到预期路径: {ds.get('ready_path')}",
            finished_at=time.time(),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("下载 %s 失败", family)
        store.set_download(family, status="error", message=f"下载失败: {e}",
                           finished_at=time.time())
    finally:
        with _download_lock:
            _active.pop(family, None)


def start_download(s: Settings, store: Store, family: str) -> bool:
    with _download_lock:
        if family in _active and _active[family].is_alive():
            return False
        t = threading.Thread(target=_run_download, args=(s, store, family), daemon=True)
        _active[family] = t
        t.start()
    return True


def active_downloads() -> List[str]:
    with _download_lock:
        return [k for k, t in _active.items() if t.is_alive()]
