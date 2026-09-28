"""SQLite 持久化：任务与结果。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .settings import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,          -- accuracy | perf | sweep
    mode         TEXT NOT NULL,          -- accuracy | perf
    parent_id    TEXT,
    label        TEXT,
    note         TEXT,
    params       TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'queued',
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL,
    exit_code    INTEGER,
    run_dir      TEXT,
    error        TEXT,
    log_path     TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_parent ON jobs(parent_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    model_abbr  TEXT,
    dataset     TEXT,
    concurrency INTEGER,
    metrics     TEXT NOT NULL DEFAULT '{}',
    artifacts   TEXT NOT NULL DEFAULT '{}',
    error       TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_job ON results(job_id);

CREATE TABLE IF NOT EXISTS downloads (
    family      TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'idle',
    progress    REAL DEFAULT 0,
    message     TEXT,
    started_at  REAL,
    finished_at REAL
);
"""


class Store:
    def __init__(self, s: Settings):
        self.path = s.db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    # 建表之后补的列。CREATE TABLE IF NOT EXISTS 不会给**已有的**库加列，
    # 所以每次启动都要对一遍：老库（客户那边已经有数据的）必须就地升上来，
    # 不能指望用户删库重来。
    _ADDED_COLUMNS = [("jobs", "note", "TEXT")]

    def _init(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)
            for table, column, decl in self._ADDED_COLUMNS:
                cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # ------------------------------------------------------------ jobs
    def create_job(
        self,
        kind: str,
        mode: str,
        params: Dict[str, Any],
        label: str = "",
        parent_id: str | None = None,
        status: str = "queued",
        job_id: str | None = None,
        note: str = "",
    ) -> str:
        jid = job_id or uuid.uuid4().hex[:12]
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO jobs (id, kind, mode, parent_id, label, note, params,"
                " status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (jid, kind, mode, parent_id, label, note,
                 json.dumps(params, ensure_ascii=False), status, time.time()),
            )
        return jid

    def set_note(self, jid: str, note: str) -> None:
        """写备注（只写这一条）。传空字符串就是清除。

        扫描任务的子任务**不复制**父任务的备注 —— 备注只在父任务上存一份，
        读取时若子任务自己没有就回退到父任务的（见 `resolve_notes`）。
        不这么做的原因：下发是一次性的，父任务事后改备注时子任务不会跟着变，
        两边就不一致了。读时兜底没有这个问题。
        """
        with self._lock, self._conn() as c:
            c.execute("UPDATE jobs SET note=? WHERE id=?",
                      ((note or "").strip(), jid))

    def resolve_notes(self, jobs: List[Dict[str, Any]]) -> None:
        """给子任务补上父任务的备注（就地修改）。

        扫描的备注是给**整组**写的，而对比页里逐条列的是子任务，
        不兜底的话对比时还是认不出哪条是哪条。

        注意要连 Sweep 父任务里嵌套的 `children` 一起处理：那是另一次查询
        出来的**对象副本**，跟顶层列表里的同名任务不是同一个 dict，
        只遍历顶层的话，界面上读嵌套列表的地方（任务列表里的"2/2 档完成"那块）
        补不到备注。
        """
        flat: List[Dict[str, Any]] = []
        for j in jobs:
            flat.append(j)
            flat.extend(j.get("children") or [])
        by_id = {j["id"]: j for j in flat}
        for j in flat:
            if j.get("note") or not j.get("parent_id"):
                continue
            p = by_id.get(j["parent_id"]) or self.get_job(j["parent_id"])
            if p and p.get("note"):
                j["note"] = p["note"]
                j["note_inherited"] = True

    def update_job(self, jid: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), jid))

    def update_job_params(self, jid: str, params: Dict[str, Any]) -> None:
        """回写任务参数。

        单独开一个方法而不是直接用 update_job：params 是 JSON 列，
        直接传 dict 进去会写成一个非法的 JSON 字符串，之后 get_job 解析就崩了。
        """
        self.update_job(jid, params=json.dumps(params, ensure_ascii=False))

    def get_job(self, jid: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        return self._job_row(r) if r else None

    def list_jobs(self, limit: int = 200, parent_only: bool = False) -> List[Dict[str, Any]]:
        q = "SELECT * FROM jobs"
        if parent_only:
            q += " WHERE parent_id IS NULL"
        q += " ORDER BY created_at DESC LIMIT ?"
        with self._conn() as c:
            rows = c.execute(q, (limit,)).fetchall()
        return [self._job_row(r) for r in rows]

    def children(self, parent_id: str) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM jobs WHERE parent_id=? ORDER BY created_at", (parent_id,)
            ).fetchall()
        return [self._job_row(r) for r in rows]

    def delete_job(self, jid: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM results WHERE job_id=?", (jid,))
            c.execute("DELETE FROM jobs WHERE id=?", (jid,))

    def claim_queued(self) -> Optional[Dict[str, Any]]:
        """原子地认领一个排队中的任务，并**就地**标记为 running。

        **不能先 SELECT、再在别处 UPDATE**（原来的 `next_queued` 就是这么写的）：
        两个 worker 可能选中同一条，同一个任务就会被跑两遍 —— 两个 aisbench 进程
        同时压同一个服务，结果还会入库两次，对比页里凭空多出一条"看起来像
        另一次运行"的记录。并发度设成 2 时实测复现过（一条任务出两条结果）。

        默认并发度是 1，只有一个 worker，撞不上；但 `max_concurrent_jobs`
        是配置项，设大了就会踩到，所以这里把「选」和「占」并成一次原子操作：
        UPDATE 带上 `status='queued'` 条件，靠 rowcount 判断有没有抢到，
        没抢到就重选下一条。
        """
        with self._lock, self._conn() as c:
            for _ in range(50):
                r = c.execute(
                    "SELECT id FROM jobs WHERE status='queued'"
                    " ORDER BY created_at LIMIT 1").fetchone()
                if not r:
                    return None
                cur = c.execute(
                    "UPDATE jobs SET status='running', started_at=?"
                    " WHERE id=? AND status='queued'", (time.time(), r["id"]))
                if cur.rowcount == 1:
                    row = c.execute("SELECT * FROM jobs WHERE id=?", (r["id"],)).fetchone()
                    return self._job_row(row)
                # 这条被别人抢走了，重选下一条
        # 连续 50 次都被抢走：说明别的 worker 很忙，交给下一轮循环
        return None

    def next_queued(self) -> Optional[Dict[str, Any]]:
        """只读地看一眼队首（不改状态）。仅用于展示/诊断，不要拿它去执行任务。"""
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
        return self._job_row(r) if r else None

    def running_jobs(self) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM jobs WHERE status='running'").fetchall()
        return [self._job_row(r) for r in rows]

    @staticmethod
    def _job_row(r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        try:
            d["params"] = json.loads(d.get("params") or "{}")
        except json.JSONDecodeError:
            d["params"] = {}
        return d

    # ------------------------------------------------------------ results
    def add_result(
        self,
        job_id: str,
        kind: str,
        model_abbr: str,
        dataset: str,
        metrics: Dict[str, Any],
        artifacts: Dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        conc = metrics.get("max_concurrency") or metrics.get("_concurrency")
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO results (job_id, kind, model_abbr, dataset, concurrency,"
                " metrics, artifacts, error, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, kind, model_abbr, dataset, int(conc) if conc else None,
                 json.dumps(metrics, ensure_ascii=False),
                 json.dumps(artifacts or {}, ensure_ascii=False),
                 error, time.time()),
            )

    def replace_results(self, job_id: str, records: List[Dict[str, Any]]) -> int:
        """用重新解析的结果替换该任务的旧记录。

        用途：产物解析逻辑修好后，不必重跑耗时的压测就能刷新已有数据。
        """
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM results WHERE job_id=?", (job_id,))
        for rec in records:
            self.add_result(
                job_id, rec.get("kind", "perf"), rec.get("model_abbr", ""),
                rec.get("dataset", ""), rec.get("metrics", {}),
                rec.get("artifacts", {}), rec.get("error"),
            )
        return len(records)

    def results_for_job(self, job_id: str) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM results WHERE job_id=? ORDER BY id", (job_id,)
            ).fetchall()
        return [self._res_row(r) for r in rows]

    def list_results(self, kinds: List[str] | None = None, job_ids: List[str] | None = None) -> List[Dict[str, Any]]:
        q = ("SELECT r.*, j.label AS job_label, j.note AS job_note,"
             " j.parent_id AS job_parent, j.params AS job_params, j.mode AS job_mode"
             " FROM results r LEFT JOIN jobs j ON j.id = r.job_id WHERE 1=1")
        args: List[Any] = []
        if kinds:
            q += f" AND r.kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        if job_ids:
            q += f" AND r.job_id IN ({','.join('?' * len(job_ids))})"
            args += job_ids
        q += " ORDER BY r.created_at DESC"
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = self._res_row(r)
            try:
                d["job_params"] = json.loads(d.get("job_params") or "{}")
            except json.JSONDecodeError:
                d["job_params"] = {}
            out.append(d)

        # 子任务没备注时回退到父任务的。这里也要做一遍：对比页的运行列表读的是
        # 这个接口，只让 compare 接口回退的话，列表和图表里的名字会对不上。
        out = self._fill_parent_notes(out, key="job_note", parent_key="job_parent")
        return out

    def _fill_parent_notes(self, rows: List[Dict[str, Any]], key: str,
                           parent_key: str) -> List[Dict[str, Any]]:
        cache: Dict[str, str] = {}
        for d in rows:
            if d.get(key) or not d.get(parent_key):
                continue
            pid = d[parent_key]
            if pid not in cache:
                p = self.get_job(pid)
                cache[pid] = ((p or {}).get("note") or "").strip()
            if cache[pid]:
                d[key] = cache[pid]
        return rows

    @staticmethod
    def _res_row(r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        for k in ("metrics", "artifacts"):
            try:
                d[k] = json.loads(d.get(k) or "{}")
            except json.JSONDecodeError:
                d[k] = {}
        return d

    # ------------------------------------------------------------ downloads
    def set_download(self, family: str, **fields: Any) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO downloads (family, status) VALUES (?, 'idle')", (family,)
            )
            if fields:
                cols = ", ".join(f"{k}=?" for k in fields)
                c.execute(f"UPDATE downloads SET {cols} WHERE family=?", (*fields.values(), family))

    def get_downloads(self) -> Dict[str, Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM downloads").fetchall()
        return {r["family"]: dict(r) for r in rows}
