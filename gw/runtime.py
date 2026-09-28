"""容器内运行时自检。

网关就跑在 aisbench 容器里，所有命令直接在容器内执行，
不再需要 docker CLI、docker socket，也不再需要 docker exec 那一层。

**这个模块以前叫 `container.py`**，干的是用 docker 创建/启动/重建容器。
改成容器内运行后那些职责全部消失（容器生命周期归 start.sh 管），
只剩"确认自己跑在一个能干活的环境里"。

客户机器因此只需要 Docker，不需要 python3。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .settings import Settings


def ais_bench_path() -> Optional[str]:
    """ais_bench 可执行文件路径。"""
    return shutil.which("ais_bench")


def ais_bench_version() -> Optional[str]:
    """探测 ais_bench 版本，拿不到就返回 None。

    aisbench **没有 --version 参数** —— 传了它会打印整段 usage。
    所以这里用 --help 的输出判断它是否可用，版本号能从输出里抠到就抠，
    抠不到就只报"可用"，不要把那坨 usage 塞进界面。
    """
    exe = ais_bench_path()
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "--help"], capture_output=True, text=True, timeout=30, check=False,
        )
        out = f"{r.stdout or ''}\n{r.stderr or ''}"
        # 形如 "ais_bench 3.1" / "AISBench v3.1" 之类的版本串，有就用
        m = re.search(r"\b(\d+\.\d+(?:\.\d+)?)\b", out)
        if m and "usage:" not in out.split(m.group(1))[0][-30:]:
            return m.group(1)
        return "可用" if "usage:" in out else None
    except Exception:  # noqa: BLE001
        return None


def _writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / f".gw_write_probe_{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def disk_free_gb(p: Path) -> Optional[float]:
    try:
        return round(shutil.disk_usage(str(p)).free / 1e9, 1)
    except OSError:
        return None


def selfcheck(s: Settings) -> Dict[str, Any]:
    """返回一组健康信息，供前端状态标签和 /api/selfcheck 使用。

    `ok` 表示能不能干活（数据目录可写 + ais_bench 在）。
    其余字段是诊断信息，缺了不至于让服务不可用。
    """
    exe = ais_bench_path()
    data_ok = _writable(s.data_dir)

    checks = {
        "data_dir": str(s.data_dir),
        "data_writable": data_ok,
        "ais_bench": exe,
        "ais_bench_version": ais_bench_version(),
        "python": sys.version.split()[0],
        "datasets_dir": str(s.datasets_dir),
        "datasets_ready": s.datasets_dir.exists(),
        "models_dir": str(s.models_dir),
        "disk_free_gb": disk_free_gb(s.data_dir),
        "checked_at": time.time(),
    }

    problems = []
    if not data_ok:
        problems.append(f"数据目录不可写：{s.data_dir}")
    if not exe:
        problems.append("找不到 ais_bench 可执行文件")

    checks["problems"] = problems
    checks["ok"] = not problems
    return checks


def require_ok(s: Settings) -> None:
    """启动时调用：环境不可用就直接报清楚，不要带着病跑到一半才炸。"""
    info = selfcheck(s)
    if info["ok"]:
        return
    raise RuntimeError(
        "运行环境不完整：\n  - " + "\n  - ".join(info["problems"]) +
        "\n请确认容器是按 start.sh 的方式启动的（数据目录挂到了 "
        f"{s.data_dir}）。"
    )
