"""从 vLLM 的 `/metrics` 上读取 prefix cache 命中率。

口径照搬参考项目 [aisbench_auto_tools_prefix](https://github.com/rayn-zzz/aisbench_auto_tools_prefix)
的 `cal_prefix_hit_rate.py`：**在 aisbench 跑之前和跑之后各取一次快照，用增量算命中率**。

    命中率 = (hits_after - hits_before) / (queries_after - queries_before)

为什么要取增量：这两个指标是进程启动以来的**累计值**，直接读它包含了此前所有请求
（包括上一次测试、以及其他人在同一个服务上跑的请求），拿它当本次命中率是错的。

按 `(pod, engine)` 分别算再汇总。分 engine 是因为一个 pod 下可能有多个 DP 域，
各自的 KV cache 是独立的 —— 混在一起看不出"某个域根本没吃到前缀"这种情况，
而那恰恰是预热没做对时的典型症状。

用标准库 `urllib` 而不是参考项目用的 `requests`：网关必须零第三方依赖，
理由见 settings.py 开头那段。
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener

log = logging.getLogger("gw.metrics")

# 指标名。前两个是 vLLM 原生的，后两个出现在带外部 KV cache（PD 分离）的版本上。
M_NORMAL_Q = "vllm:prefix_cache_queries_total"
M_NORMAL_H = "vllm:prefix_cache_hits_total"
M_EXT_Q = "external_prefix_cache_queries_total"
M_EXT_H = "external_prefix_cache_hits_total"

_ENGINE_RE = re.compile(r'engine="(\d+)"')

DEFAULT_TIMEOUT = 10.0


# ---------------------------------------------------------------- 端点

def normalize_pod(raw: str) -> Optional[str]:
    """把用户写的端点规整成 `http://host:port`。

    接受 `192.168.1.10:8000`、`http://192.168.1.10:8000`、`[::1]:8000` 三种写法。
    没写协议就补 http —— vLLM 的 metrics 跟服务同端口，一般就是 http。
    """
    s = (raw or "").strip().rstrip("/")
    if not s:
        return None
    if "://" not in s:
        s = "http://" + s
    u = urlparse(s)
    if not u.hostname:
        return None
    port = u.port or (443 if (u.scheme or "http") == "https" else 80)
    host = u.hostname
    if ":" in host:            # IPv6 字面量
        host = f"[{host}]"
    return f"{u.scheme or 'http'}://{host}:{port}"


def pod_from_base_url(base_url: str) -> Optional[str]:
    """默认拿被测服务自己的地址当 metrics 端点。

    PD 混部（一个服务同时做 prefill 和 decode）填这个就够，
    所以绝大多数情况下用户不需要额外配置。
    """
    return normalize_pod(base_url or "")


def resolve_pods(configured: List[str], base_url: str) -> List[str]:
    """决定这次要查哪些端点。

    配置了就用配置的（PD 分离时是各个 P 节点的 IP 和对应 DP 域的端口），
    没配置就用被测服务自己。去重并保持顺序。
    """
    out: List[str] = []
    for raw in configured or []:
        p = normalize_pod(raw)
        if p and p not in out:
            out.append(p)
    if out:
        return out
    p = pod_from_base_url(base_url)
    return [p] if p else []


# ---------------------------------------------------------------- 取数

def _fetch(url: str, timeout: float) -> str:
    # **必须绕过代理**：环境里若有 http_proxy，urllib 会把内网地址也走代理，
    # 而代理一般连不到内网，表现为超时或 502。参考项目里也专门关了这个。
    opener = build_opener(ProxyHandler({}))
    with opener.open(url, timeout=timeout) as resp:      # noqa: S310  地址来自用户配置
        return resp.read().decode("utf-8", errors="replace")


def _parse(text: str, metric: str) -> Dict[int, float]:
    """从 Prometheus 文本里取出某个指标按 engine 分组的当前值。

    只认带 `engine="N"` 标签的行 —— 没有 engine 标签就没法归到具体的 DP 域，
    而分域正是我们要的。参考项目还额外要求行里有 `model_name`；
    这里不强制：有些版本只有 engine 没有 model_name，强制要求会一条都取不到。
    真出现同名指标跨模型混在一起的情况，值会覆盖，那种部署本来就少见。
    """
    out: Dict[int, float] = {}
    for line in text.splitlines():
        if metric not in line or line.startswith("#"):
            continue
        m = _ENGINE_RE.search(line)
        if not m:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            out[int(m.group(1))] = float(parts[-1])
        except ValueError:
            continue
    return out


def _snapshot_one(pod: str, timeout: float) -> Tuple[str, Dict[str, Any]]:
    try:
        text = _fetch(f"{pod}/metrics", timeout)
    except Exception as e:  # noqa: BLE001  取不到就跳过，不该拖垮整个任务
        return pod, {"error": str(e), "engines": {}}

    engines: Dict[int, Dict[str, float]] = {}
    for key, metric in (("queries", M_NORMAL_Q), ("hits", M_NORMAL_H),
                        ("ext_queries", M_EXT_Q), ("ext_hits", M_EXT_H)):
        for engine, value in _parse(text, metric).items():
            engines.setdefault(engine, {})[key] = value
    return pod, {"engines": engines}


def snapshot(pods: List[str], timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """并发取所有端点的当前累计值。取不到不抛异常，只记在结果里。"""
    out: Dict[str, Any] = {}
    if not pods:
        return out
    with ThreadPoolExecutor(max_workers=max(1, len(pods))) as ex:
        for pod, data in ex.map(lambda p: _snapshot_one(p, timeout), pods):
            out[pod] = data
    return out


# ---------------------------------------------------------------- 计算

def _delta(after: Optional[float], before: Optional[float]) -> Optional[float]:
    """增量。缺任何一边就返回 None —— 不拿一个数当两个用。"""
    if after is None or before is None:
        return None
    return after - before


def _rate(hits: Optional[float], queries: Optional[float]) -> Optional[float]:
    if hits is None or queries is None or queries <= 0:
        return None
    return hits / queries


def _pair(before: Dict[str, float], after: Dict[str, float],
          hk: str, qk: str) -> Dict[str, Any]:
    h = _delta(after.get(hk), before.get(hk))
    q = _delta(after.get(qk), before.get(qk))
    return {"hits": h, "queries": q, "rate": _rate(h, q)}


def hit_rate(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """按 (pod, engine) 求增量命中率，并汇总成 ALL_PODS。

    某个端点两次快照的 engine 集合对不上、或者根本没取到数，就跳过它并记进
    `skipped` —— 硬算会得出一个看起来正常但其实是错的数字，那比没有数字更糟。
    参考项目在这里也是直接跳过（它要求八类指标齐全且 engine 集合一致）。
    """
    pods: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    tot = {"hits": 0.0, "queries": 0.0, "ext_hits": 0.0, "ext_queries": 0.0}
    counted = 0

    for pod in sorted(before.keys() | after.keys()):
        b, a = before.get(pod) or {}, after.get(pod) or {}
        be, ae = b.get("engines") or {}, a.get("engines") or {}

        if b.get("error") or a.get("error"):
            skipped.append({"pod": pod, "reason": b.get("error") or a.get("error") or ""})
            continue
        if not be or set(be) != set(ae):
            skipped.append({
                "pod": pod,
                "reason": "两次快照的 engine 集合不一致或为空"
                          f"（前 {sorted(be)} / 后 {sorted(ae)}），无法求增量",
            })
            continue

        engines = []
        for eid in sorted(be):
            row = {
                "engine": eid,
                "hbm": _pair(be[eid], ae[eid], "hits", "queries"),
                "external": _pair(be[eid], ae[eid], "ext_hits", "ext_queries"),
            }
            engines.append(row)
            for key, src in (("hits", row["hbm"]["hits"]), ("queries", row["hbm"]["queries"]),
                             ("ext_hits", row["external"]["hits"]),
                             ("ext_queries", row["external"]["queries"])):
                if src is not None:
                    tot[key] += src
        counted += 1

        pod_h = sum(r["hbm"]["hits"] or 0 for r in engines)
        pod_q = sum(r["hbm"]["queries"] or 0 for r in engines)
        pod_eh = sum(r["external"]["hits"] or 0 for r in engines)
        pod_eq = sum(r["external"]["queries"] or 0 for r in engines)
        pods.append({
            "pod": pod,
            "engines": engines,
            "total": {
                "hbm": {"hits": pod_h, "queries": pod_q, "rate": _rate(pod_h, pod_q)},
                "external": {"hits": pod_eh, "queries": pod_eq,
                             "rate": _rate(pod_eh, pod_eq)},
            },
        })

    total = {
        "hbm": {"hits": tot["hits"], "queries": tot["queries"],
                "rate": _rate(tot["hits"], tot["queries"])},
        "external": {"hits": tot["ext_hits"], "queries": tot["ext_queries"],
                     "rate": _rate(tot["ext_hits"], tot["ext_queries"])},
    }
    info: Dict[str, Any] = {"ok": counted > 0, "pods": pods, "total": total,
                            "skipped": skipped}

    # Δqueries 为 0 是个**诊断信号**，不是"命中率 0%"：指标在整个测试期间
    # 一点没动，说明这些请求根本没打到这个服务（地址填错、走了别的实例、
    # 或者前面挂了负载均衡），也可能该服务压根没有 prefix cache。
    # 不点出来的话，用户会对着一个空白或 0% 猜半天。
    if counted and not tot["queries"] and not tot["ext_queries"]:
        info["note"] = ("所有端点的累积指标在整个测试期间都没有增长（Δqueries=0）："
                        "请求可能没打到你查的这个服务，或该服务没有 prefix cache。"
                        "请确认 metrics 端点与被测服务是同一个实例。")
    return info


# ---------------------------------------------------------------- 展示

def _fmt_rate(rate: Optional[float]) -> str:
    return "—" if rate is None else f"{rate:.2%}"


def _fmt_pair(d: Dict[str, Any]) -> str:
    """只输出 (命中/查询) 明细 —— 命中率本身有单独一列，别重复。"""
    h, q = d.get("hits"), d.get("queries")
    if not q:                       # None 或 0 都算"没有数据"
        return "—"
    return f"({int(h)}/{int(q)})"


def format_table(info: Dict[str, Any]) -> str:
    """把结果排成一段对齐的文本，写进任务日志。

    对齐要自己算宽度：中文注释和英文 scope 混排时，
    f-string 的宽度参数对中文是按字符算的，直接用会错位。
    """
    rows: List[Tuple[str, str, str, str, str]] = []
    for pod in info.get("pods") or []:
        for eng in pod["engines"]:
            rows.append((
                f"{pod['pod']}/engine{eng['engine']}",
                _fmt_rate(eng["hbm"].get("rate")), _fmt_pair(eng["hbm"]),
                _fmt_rate(eng["external"].get("rate")), _fmt_pair(eng["external"]),
            ))
        rows.append((f"{pod['pod']} 小计",
                     _fmt_rate(pod["total"]["hbm"].get("rate")), _fmt_pair(pod["total"]["hbm"]),
                     _fmt_rate(pod["total"]["external"].get("rate")),
                     _fmt_pair(pod["total"]["external"])))

    t = info.get("total") or {}
    rows.append(("ALL_PODS",
                 _fmt_rate((t.get("hbm") or {}).get("rate")), _fmt_pair(t.get("hbm") or {}),
                 _fmt_rate((t.get("external") or {}).get("rate")),
                 _fmt_pair(t.get("external") or {})))

    headers = ("scope", "HBM 命中率", "HBM (命中/查询)", "外部命中率", "外部 (命中/查询)")
    cols = list(zip(*([headers] + [(r[0], r[1], r[2], r[3], r[4]) for r in rows])))
    widths = [max(_w(c) for c in col) for col in cols]

    def line(cells) -> str:
        return "  ".join(_pad(c, w) for c, w in zip(cells, widths)).rstrip()

    sep = "-" * (sum(widths) + 2 * (len(widths) - 1))
    out = ["=" * len(sep), "Prefix cache 命中率（本次测试期间，按 /metrics 增量计算）",
           "=" * len(sep), line(headers), sep]
    out += [line(r) for r in rows]
    out.append(sep)
    for s in info.get("skipped") or []:
        out.append(f"跳过 {s['pod']}：{s['reason']}")
    if info.get("note"):
        out.append(info["note"])
    if not info.get("ok"):
        out.append("没有取到任何有效的 prefix cache 指标。"
                   "请确认被测服务是 vLLM 且暴露了 /metrics"
                   "（curl http://<ip>:<port>/metrics | grep prefix）。")
    return "\n".join(out)


def _w(s: str) -> int:
    """显示宽度：中日韩字符占两格。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _w(s))


# ---------------------------------------------------------------- 落库

def summary_metrics(info: Dict[str, Any]) -> Dict[str, Any]:
    """挑出要并进性能结果的几个标量，让对比页能直接画/比。

    只放标量：对比页的指标选择器是按数值画柱状/折线，塞嵌套结构进去没法用。
    """
    if not (info or {}).get("ok"):
        return {}
    t = info.get("total") or {}
    hbm, ext = t.get("hbm") or {}, t.get("external") or {}
    # 只在真的有增量时才写入。查不到 / 增量为 0 时写个 0 进去，
    # 在对比图上会显示成"命中率 0%"，那是个会被误读成结论的数字。
    if not hbm.get("queries"):
        return {}
    out: Dict[str, Any] = {
        "prefix_hit_rate": hbm.get("rate"),
        "prefix_queries": hbm.get("queries"),
        "prefix_hits": hbm.get("hits"),
    }
    if ext.get("queries"):
        out["prefix_external_hit_rate"] = ext.get("rate")
    return {k: v for k, v in out.items() if v is not None}
