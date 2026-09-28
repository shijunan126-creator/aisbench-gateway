"""任务队列与执行。

**串行执行**：性能压测任务不能并行跑，否则多个任务会互相抢模型服务，
把吞吐数据污染成没有意义的数字。max_concurrent_jobs 默认 1。

并发梯度扫描（sweep）在这里落地：一个父任务 + N 个子任务（每个并发档位一个），
子任务串行跑完后由父任务汇总，前端按并发为 x 轴画曲线。
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import catalog, confgen, dataset_gen, metrics, results
from .settings import Settings
from .store import Store

log = logging.getLogger("gw.runner")

_EXP_FOLDER_RE = re.compile(r"Current exp folder:\s*(\S+)")


def _gen_abbr(spec: "dataset_gen.Spec") -> str:
    """合成数据集在结果表/图例里显示的名字。

    参数不同名字就不同：同一个任务里跑了 50% 和 90% 两档前缀，
    曲线得能区分开，否则对比时全挤成一个名字。
    """
    if spec.variable_length:
        if spec.length_mean is not None and spec.length_std is not None:
            shape = "g%d+-%g" % (spec.length_mean, spec.length_std)
        else:
            shape = "%d-%d" % (spec.length_min or 0, spec.length_max or 0)
    else:
        shape = "in%d" % spec.input_len
    if spec.uses_prefix:
        return "prefix%d-%s" % (int(round(spec.prefix_ratio * 100)), shape)
    return "len-%s" % shape

# 子任务串行结束、父任务汇总前要等的状态
_TERMINAL = {"succeeded", "failed", "cancelled"}


class Runner:
    def __init__(self, s: Settings, store: Store):
        self.s = s
        self.store = store
        self._threads: List[threading.Thread] = []
        self._procs: Dict[str, subprocess.Popen] = {}
        self._cancel: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        n = max(1, self.s.max_concurrent_jobs)
        if n > 1:
            # 任务本身不会再被重复执行了（claim_queued 是原子的），但**并发跑压测**
            # 会让两个任务抢同一个模型服务，把吞吐数据互相污染成没有意义的数字。
            # 精度测试多开几个无所谓，压测务必保持 1。
            log.warning(
                "max_concurrent_jobs=%d > 1：多个任务会**同时**压同一个模型服务，"
                "吞吐/时延数据会互相污染。压测请设回 1（只有精度测试适合调大）。", n)
        for i in range(n):
            t = threading.Thread(target=self._worker_loop, name=f"gw-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("任务执行器已启动，并发度 %d", n)

    def shutdown(self) -> None:
        self._stop.set()
        for jid, p in list(self._procs.items()):
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 提交
    def submit(self, params: Dict[str, Any], mode: str, note: str = "") -> Dict[str, Any]:
        """提交任务。mode=perf 且给了并发档位列表时自动拆成扫描。"""
        if mode == "perf":
            concs = params.get("concurrency_list")
            if isinstance(concs, str):
                concs = [c.strip() for c in concs.split(",") if c.strip()]
            if concs:
                return self._submit_sweep(params, [int(c) for c in concs], note=note)
            return self._submit_single(params, "perf", note=note)

        if mode == "accuracy":
            return self._submit_single(params, "accuracy", note=note)

        raise ValueError(f"未知模式: {mode}")

    def _submit_single(self, params: Dict[str, Any], mode: str, parent: str | None = None,
                       label: str = "", note: str = "") -> str:
        p = dict(params)
        p["_data_dir"] = self.s.data_path
        kind = "accuracy" if mode == "accuracy" else "perf"
        jid = self.store.create_job(kind, mode, p, label=label, parent_id=parent, note=note)
        return jid

    def _submit_sweep(self, params: Dict[str, Any], concs: List[int],
                      note: str = "") -> Dict[str, Any]:
        p = dict(params)
        p["_data_dir"] = self.s.data_path
        # 父任务只做汇总，不真正执行
        pid = self.store.create_job(
            "sweep", "perf", p, label=f"并发扫描 {concs}", status="running",
        )
        self.store.update_job(pid, started_at=time.time())
        child_ids = []
        for c in concs:
            cp = dict(p)
            cp["concurrency"] = c
            cp.pop("concurrency_list", None)
            child_ids.append(
                self._submit_single(cp, "perf", parent=pid, label=f"并发 {c}")
            )
        # 备注只写在父任务上，子任务读取时回退（见 store.resolve_notes）。
        # 不下发到子任务：父任务事后改备注时子任务不会跟着变，两边会不一致。
        if note:
            self.store.set_note(pid, note)
        return {"parent_id": pid, "job_ids": child_ids}

    def delete(self, jid: str) -> bool:
        """删除任务记录（磁盘产物保留）。

        任务在跑就先取消，否则 worker 还在用这条记录，删掉之后它收尾时
        会去 update 一个不存在的行（静默无效），日志/状态就全乱了。

        扫描任务要**连子任务一起删**：子任务是被 worker 按 `status='queued'`
        捞出来执行的，只删父任务的话，剩下那些排队中的子任务照样会跑起来，
        跑完还带着一个指向已删除父任务的 parent_id。
        """
        job = self.store.get_job(jid)
        if not job:
            return False
        if job["status"] not in _TERMINAL:
            self.cancel(jid)
            # 等 worker 真正收手（杀进程是异步的），最多等几秒
            for _ in range(50):
                if (self.store.get_job(jid) or {}).get("status") in _TERMINAL:
                    break
                time.sleep(0.1)

        if job["kind"] == "sweep":
            for c in self.store.children(jid):
                if c["status"] not in _TERMINAL:
                    self.cancel(c["id"])
                self.store.delete_job(c["id"])
        self.store.delete_job(jid)
        return True

    # ------------------------------------------------------------ 取消/删除
    def cancel(self, jid: str) -> bool:
        job = self.store.get_job(jid)
        if not job:
            return False
        if job["status"] in _TERMINAL:
            return False
        self._cancel.add(jid)
        if job["kind"] == "sweep":
            for c in self.store.children(jid):
                if c["status"] in ("queued", "running"):
                    self.cancel(c["id"])
        with self._lock:
            proc = self._procs.get(jid)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self._kill_job_processes(jid)
        if job["status"] == "queued":
            self.store.update_job(jid, status="cancelled", finished_at=time.time())
            # 排队中被取消的任务**永远不会被 worker 领走**，所以 _execute 里的收尾
            # 逻辑不会跑到 —— 扫描父任务就没人去汇总结状态，会永远卡在 running。
            # 这里补一次。（实测：取消一个 3 档扫描，子任务全 cancelled、
            # 父任务 80 秒后仍是 running。）
            self._maybe_finalize_parent(job.get("parent_id"))
        return True

    def _kill_job_processes(self, jid: str) -> None:
        """杀掉该任务启动的整个进程组。

        aisbench 会 fork 出工作子进程（openicl_api_infer.py 等），只杀父进程
        会留下孤儿继续占着模型服务；而单 worker 串行时后面的任务会被一直堵住。
        所以 Popen 时用 start_new_session=True 让每个任务自成进程组，这里整组杀。

        以前网关在宿主机上、任务跑在容器里，只能靠 `docker exec pkill` 兜；
        现在网关就在容器内，直接对进程组下手，干净得多。
        """
        proc = None
        with self._lock:
            proc = self._procs.get(jid)
        if not proc or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # ------------------------------------------------------------ 工作循环
    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            # claim_queued 会**就地**把任务标成 running，是原子的。
            # 别改成「先 next_queued 看一眼、再交给 _execute 去标 running」——
            # 那中间有窗口，并发度 >1 时两个 worker 会抢到同一条任务并各跑一遍。
            job = self.store.claim_queued()
            if not job:
                time.sleep(1.0)
                continue
            try:
                self._execute(job)
            except Exception as e:  # noqa: BLE001
                log.exception("任务 %s 执行异常", job["id"])
                self.store.update_job(
                    job["id"], status="failed", error=str(e), finished_at=time.time()
                )

    def _log_path(self, jid: str) -> Path:
        d = self.s.outputs_dir / jid
        d.mkdir(parents=True, exist_ok=True)
        return d / "run.log"

    def _generate_dataset(self, job: Dict[str, Any], log_path: Path) -> None:
        """合成数据集，并把产物路径写回任务参数。

        写回是必须的：confgen 要拿这个路径去构造配置，而路径里带着参数哈希，
        调用方算不出来（得跟 dataset_gen 保持一致），所以由生成方回填。
        """
        p = job["params"]
        spec = dataset_gen.spec_from_params(p)

        with log_path.open("ab") as logf:

            def progress(msg: str) -> None:
                logf.write(f"[gateway] {msg}\n".encode())
                logf.flush()

            progress(f"开始生成数据集：{spec.summary()}")
            res = dataset_gen.generate(
                spec,
                p.get("tokenizer_path", ""),
                self.s.datasets_dir,
                self.s.generated_dir,
                progress=progress,
                # 生成是阻塞的 CPU 活，只能逐行协作式地看取消标志。
                # 不传的话，取消一个长上下文生成要等好几分钟才有反应。
                should_stop=lambda: job["id"] in self._cancel,
            )

        p["_gen_dataset_path"] = str(res.dataset_path)
        p["_gen_prefix_path"] = str(res.prefix_path) if res.prefix_path else None
        p["_gen_abbr"] = _gen_abbr(spec)
        p["_gen_summary"] = spec.summary()
        p["_gen_dp"] = spec.dp
        # 没有前缀池就没有可预热的东西（前缀比例为 0 时）
        p["_gen_warmup"] = bool(p.get("gen_warmup")) and bool(res.prefix_path)
        p["_gen_stats"] = res.stats
        p["_gen_cached"] = res.cached
        self.store.update_job_params(job["id"], p)

        # 顺手把任务标签换成带参数指纹的。
        # 默认标签是「GSM8K 前缀数据集（…）· 性能」，所有合成数据集任务长得一模一样，
        # 而对比不同前缀比例（50% vs 75%、2k vs 32k）正是这个功能的主要用法 ——
        # 标签不区分的话，图和表里根本认不出哪条是哪条。
        #
        # 扫描子任务不动：它们的「并发 N」本身就有区分度，而且是曲线要用的标签。
        if not job.get("parent_id"):
            ds = catalog.get_dataset(p.get("dataset")) or {}
            self.store.update_job(
                job["id"],
                label=f"{ds.get('label') or p.get('dataset')} · {p['_gen_abbr']}",
            )

    def _run_stage(self, job: Dict[str, Any], st: Dict[str, Any],
                   logf: Any) -> Dict[str, Any]:
        """跑一次 aisbench。

        返回 `{"ok": True, "run_dir": ...}`，或 `{"cancelled": True}`，
        或 `{"ok": False, "error": ..., "rc": ..., "run_dir": ...}`。
        调用方按这个决定是继续下一阶段还是收尾。
        """
        jid = job["id"]
        stage = st["stage"]

        # 预热阶段要用不同的并发/限速，所以复制一份参数改掉，
        # 不能就地改 —— 下一个阶段（正式测试）还得用原来的值。
        params = dict(job.get("params") or {})
        if st.get("concurrency"):
            params["concurrency"] = int(st["concurrency"])
        if st.get("request_rate") is not None:
            params["request_rate"] = int(st["request_rate"])
        stage_job = {**job, "params": params}

        try:
            confgen.write_config(self.s, stage_job,
                                 dataset_path=st.get("dataset_path"), stage=stage)
            argv = confgen.build_argv(self.s, stage_job, stage=stage)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"生成配置失败: {e}"}

        cmd = argv
        logf.write(f"$ {' '.join(cmd)}\n".encode())
        logf.flush()

        # prefix cache 的累计值要在测试**前后各取一次**，差值才是本次的命中情况。
        # 取在 aisbench 进程之外，不会挤占压测窗口。
        pods: List[str] = st.get("pods") or []
        prefix_before: Optional[Dict[str, Any]] = None
        if pods:
            logf.write(f"[gateway] 采集 prefix cache 快照（前）：{', '.join(pods)}\n".encode())
            logf.flush()
            prefix_before = metrics.snapshot(pods)
            for pod, data in prefix_before.items():
                if data.get("error"):
                    # 提前说一声：地址填错的话用户不用等整轮压测跑完才知道
                    logf.write(f"[gateway]   取不到 {pod}/metrics：{data['error']}\n".encode())
            logf.flush()

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                start_new_session=True,   # 自成进程组，便于整组终止
                # **必须显式指定工作目录**：aisbench 的 LocalRunner 会创建
                # 相对路径的 `tmp/`（runners/local.py::_launch 里的
                # mmengine.mkdir_or_exist('tmp/')）。网关自己的 cwd 是
                # /gateway（为了 `python3 -m gw.main` 能解析模块），而它是
                # 只读挂载，aisbench 在那里建目录会直接
                #   OSError: [Errno 30] Read-only file system: 'tmp/'
                # 然后卡住不退出。放到数据目录下就正常了。
                cwd=str(self.s.data_dir),
            )
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"启动失败: {e}"}

        with self._lock:
            self._procs[jid] = proc

        run_dir: Optional[Path] = None
        assert proc.stdout is not None

        # 用一个读取线程把 stdout 推进队列，主循环才有机会做超时判断。
        # 直接 `for line in proc.stdout` 会阻塞在 readline 上，进程卡死时
        # 这个 worker 线程就永远醒不过来了。
        q: "queue.Queue[Any]" = queue.Queue()
        SENTINEL = object()

        def _reader(pipe=proc.stdout) -> None:
            try:
                for ln in iter(pipe.readline, b""):
                    q.put(ln)
            finally:
                q.put(SENTINEL)

        threading.Thread(target=_reader, daemon=True).start()

        started = time.time()
        last_output = started
        timed_out: Optional[str] = None

        while True:
            try:
                raw = q.get(timeout=1.0)
            except queue.Empty:
                raw = None

            if raw is SENTINEL:
                break
            if raw:
                logf.write(raw)
                logf.flush()
                last_output = time.time()
                m = _EXP_FOLDER_RE.search(raw.decode("utf-8", errors="replace"))
                if m:
                    run_dir = Path(m.group(1))

            now = time.time()
            if self.s.job_timeout and now - started > self.s.job_timeout:
                timed_out = f"超过硬超时 {self.s.job_timeout}s，已终止"
                break
            if self.s.stall_timeout and now - last_output > self.s.stall_timeout:
                timed_out = (f"日志超过 {self.s.stall_timeout}s 无任何新增，判定卡死并终止。"
                             "可查看任务日志确认是服务端无响应还是 aisbench 本身卡住；"
                             "偶发卡死时重提任务即可。")
                break
            if proc.poll() is not None and q.empty():
                break

        if timed_out:
            log.warning("任务 %s 阶段 %s %s", jid, stage, timed_out)
            logf.write(f"\n[gateway] {timed_out}\n".encode())
            logf.flush()
            self._kill_job_processes(jid)
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

        proc.wait()
        rc = proc.returncode
        with self._lock:
            self._procs.pop(jid, None)

        if jid in self._cancel:
            return {"cancelled": True, "rc": rc}
        if timed_out:
            return {"ok": False, "error": timed_out, "rc": rc, "run_dir": run_dir}
        if rc != 0:
            # 说清楚是哪一段挂的：两阶段跑起来后，
            # 光一句「退出码 1」根本分不清是预热还是正式测试失败。
            who = "预热阶段" if stage == "warmup" else "aisbench"
            return {"ok": False, "error": f"{who}退出码 {rc}", "rc": rc, "run_dir": run_dir}

        # 后快照取在进程**退出之后** —— aisbench 退出时请求都已发完并结算，
        # 早取会漏掉最后一批还在途的请求。只在成功路径上取：
        # 失败/超时/取消时这个数不会被用到，没必要再发请求（超时场景下
        # 服务可能正忙，这一下还可能卡住十几秒）。
        prefix_after = metrics.snapshot(pods) if pods else None
        return {"ok": True, "rc": rc, "run_dir": run_dir,
                "prefix_before": prefix_before, "prefix_after": prefix_after}

    def _execute(self, job: Dict[str, Any]) -> None:
        jid = job["id"]

        if jid in self._cancel:
            # 任务是在被认领之后、真正开跑之前收到取消的
            self._cancel.discard(jid)
            self.store.update_job(jid, status="cancelled", finished_at=time.time())
            self._maybe_finalize_parent(job.get("parent_id"))
            return

        # 状态已经在 claim_queued 里置成 running 了（那是原子的，见那里的说明）。
        # 这里只补 started_at 之外的收尾准备，**不要**把状态改回 queued 或
        # 试图在这里"认领"任务。
        log_path = self._log_path(jid)
        p = job.get("params") or {}

        # 合成的数据集要先落盘，配置里才能写它的路径。生成可能要几分钟
        # （128k × 大条数），进度实时写进任务日志，免得用户以为卡死了。
        if p.get("dataset") == catalog.PREFIX_GEN_FAMILY:
            try:
                self._generate_dataset(job, log_path)
            except dataset_gen.GenCancelled:
                log.info("任务 %s 在生成数据集时被取消", jid)
                self._cancel.discard(jid)
                with log_path.open("ab") as logf:
                    logf.write("[gateway] 已取消，停止生成\n".encode())
                self.store.update_job(jid, status="cancelled", finished_at=time.time())
                return
            except Exception as e:  # noqa: BLE001
                log.exception("生成数据集失败 %s", jid)
                self._finish_failed(jid, log_path, f"数据集生成失败：{e}")
                return

        # 组装要跑的阶段。
        #
        # 生成的数据集若勾了「先预热前缀」，就先拿前缀池单独跑一遍再跑正式数据集：
        # DP 域之间不共享 KV cache，不预热的话每个域都得从零算一次前缀，
        # 测出来的命中率会被稀释，反映不了稳态。这一步并发取 DP 域数、不限速，
        # 目的是把前缀灌进每个域，不是压测。
        # prefix cache 命中率只在正式阶段采集：那一轮才是要入库的结果。
        # 预热阶段的命中率没有意义（它就是在灌前缀），采了反而干扰。
        pods: List[str] = []
        if p.get("collect_metrics"):
            pods = metrics.resolve_pods(self.s.metrics_pods, p.get("base_url", ""))

        stages: List[Dict[str, Any]] = []
        if p.get("_gen_warmup") and p.get("_gen_prefix_path"):
            stages.append({
                "stage": "warmup",
                "dataset_path": p["_gen_prefix_path"],
                "concurrency": p.get("_gen_dp"),
                "request_rate": 0,
                "title": f"预热前缀（并发 {p.get('_gen_dp') or 1}，不限速）",
            })
        stages.append({"stage": "main", "dataset_path": p.get("_gen_dataset_path"),
                       "title": "正式测试", "pods": pods or None})

        run_dir: Optional[Path] = None
        prefix_before: Optional[Dict[str, Any]] = None
        prefix_after: Optional[Dict[str, Any]] = None
        with log_path.open("ab") as logf:
            for st in stages:
                # 上一阶段跑完到这一阶段开始之间也可能收到取消（比如在预热阶段点的取消）
                if jid in self._cancel:
                    self._cancel.discard(jid)
                    self.store.update_job(jid, status="cancelled", finished_at=time.time())
                    self._maybe_finalize_parent(job.get("parent_id"))
                    return
                if len(stages) > 1:
                    logf.write(f"\n[gateway] ===== {st['title']} =====\n".encode())
                    logf.flush()
                res = self._run_stage(job, st, logf)
                if res.get("cancelled"):
                    self._cancel.discard(jid)
                    self.store.update_job(jid, status="cancelled", exit_code=res.get("rc"),
                                          finished_at=time.time())
                    self._maybe_finalize_parent(job.get("parent_id"))
                    return
                if not res["ok"]:
                    self._finish_failed(jid, log_path, res["error"],
                                        exit_code=res.get("rc"), run_dir=res.get("run_dir"))
                    return
                # 预热阶段的产物只留在磁盘上供排查，不入库也不参与结果解析
                run_dir = res["run_dir"]
                if res.get("prefix_before") is not None:
                    prefix_before = res["prefix_before"]
                    prefix_after = res.get("prefix_after")

        prefix_info: Optional[Dict[str, Any]] = None
        if prefix_before is not None:
            prefix_info = metrics.hit_rate(prefix_before, prefix_after or {})
            with log_path.open("ab") as logf:
                logf.write(("\n" + metrics.format_table(prefix_info) + "\n").encode())
                logf.flush()
            p["_prefix_hit"] = prefix_info
            self.store.update_job_params(jid, p)

        rc = 0
        # 成功：解析产物
        run_dir = run_dir or self._guess_run_dir(jid)
        n = 0
        # prefix cache 命中率并进性能结果的 metrics 里，这样对比页能直接拿来画，
        # 也能和吞吐/时延放在同一张对比表里看（"命中率上去了，吞吐涨了多少"）。
        extra = metrics.summary_metrics(prefix_info) if prefix_info else {}
        parse_err: Optional[str] = None
        if run_dir and run_dir.exists():
            try:
                # artifacts_base 传产物根目录：产物接口就是按这个 base 解析的，
                # 传错会让「原生可视化」的链接全部 404（见 results.artifact_rel）
                for rec in results.collect(run_dir, job["mode"], self.s.outputs_dir):
                    self.store.add_result(
                        jid, rec.get("kind", job["mode"]), rec.get("model_abbr", ""),
                        rec.get("dataset", ""), {**rec.get("metrics", {}), **extra},
                        rec.get("artifacts", {}), rec.get("error"),
                    )
                    n += 1
            except Exception as e:  # noqa: BLE001
                log.exception("解析产物失败 %s", jid)
                parse_err = str(e)

        # 没解析到结果时要把真实原因挖出来：aisbench 的评估步骤失败时
        # 退出码仍然是 0，只报"成功"会让用户完全摸不着头脑。
        warn = None
        if not n:
            reason = results.find_failure_reason(run_dir) if run_dir else None
            warn = (
                f"aisbench 退出码为 0，但没有产生可解析的结果。日志中的错误：{reason}"
                if reason else
                "aisbench 退出码为 0，但没有产生可解析的结果，请查看运行日志"
            )
        # 解析异常比上面那句笼统的话具体得多，**不能被它盖掉** ——
        # 下面这次 update_job 是把 error 整个字段写进去的。
        if parse_err:
            warn = f"产物解析失败：{parse_err}" + (f"；{warn}" if warn else "")

        self.store.update_job(
            jid, status="succeeded", exit_code=rc, finished_at=time.time(),
            run_dir=str(run_dir) if run_dir else None, error=warn,
        )
        self._maybe_finalize_parent(job.get("parent_id"))

    def _finish_failed(self, jid: str, log_path: Path, err: str,
                       exit_code: int | None = None, run_dir: Path | None = None) -> None:
        # 光给"退出码 1"用户根本没法定位。aisbench 的报错常常非常间接 ——
        # 比如模型服务连不上时，它抛的是
        #   AISBenchDataContentError: different structure of perf data
        # 跟真正的原因（Connection refused）八竿子打不着。
        # 所以这里再从日志里把真正的异常行挖出来补进错误信息。
        if run_dir is None:
            runs = results.find_runs(self.s.outputs_dir / jid)
            run_dir = runs[-1] if runs else None

        detail = results.find_failure_reason(run_dir) if run_dir else None
        if not detail:
            detail = results.find_log_failure_reason(log_path)

        message = err if not detail else f"{err} —— {detail}"

        self.store.update_job(
            jid, status="failed", error=message, exit_code=exit_code,
            finished_at=time.time(), log_path=str(log_path),
            run_dir=str(run_dir) if run_dir else None,
        )
        self._maybe_finalize_parent(self.store.get_job(jid).get("parent_id"))  # type: ignore[union-attr]

    def _guess_run_dir(self, jid: str) -> Optional[Path]:
        runs = results.find_runs(self.s.outputs_dir / jid)
        return runs[-1] if runs else None

    def _maybe_finalize_parent(self, parent_id: Optional[str]) -> None:
        """子任务全部结束时，给扫描父任务收尾。"""
        if not parent_id:
            return
        kids = self.store.children(parent_id)
        if not kids or not all(k["status"] in _TERMINAL for k in kids):
            return

        if any(k["status"] == "succeeded" for k in kids):
            status, error = "succeeded", None
        elif all(k["status"] == "cancelled" for k in kids):
            # 全是用户取消的，不该报成「失败」——主动取消和跑挂了是两回事，
            # 报错了会让人以为扫描出问题了
            status, error = "cancelled", None
        else:
            status, error = "failed", "所有子任务均失败"
        self.store.update_job(parent_id, status=status, finished_at=time.time(),
                              error=error)

    # ------------------------------------------------------------ 扫描汇总
    def sweep_series(self, parent_id: str) -> List[Dict[str, Any]]:
        """把扫描的各并发档位整理成前端画曲线用的序列。"""
        pts: List[Dict[str, Any]] = []
        for k in self.store.children(parent_id):
            if k["status"] != "succeeded":
                continue
            conc = int((k["params"] or {}).get("concurrency") or 0)
            for r in self.store.results_for_job(k["id"]):
                m = r["metrics"]
                pts.append({
                    "job_id": k["id"],
                    "concurrency": conc,
                    "dataset": r["dataset"],
                    "request_throughput": m.get("request_throughput"),
                    "output_token_throughput": m.get("output_token_throughput"),
                    "total_token_throughput": m.get("total_token_throughput"),
                    "ttft_average": m.get("ttft_average"),
                    "ttft_p99": m.get("ttft_p99"),
                    "tpot_average": m.get("tpot_average"),
                    "tpot_p99": m.get("tpot_p99"),
                    "e2el_average": m.get("e2el_average"),
                    "e2el_p99": m.get("e2el_p99"),
                    "success_requests": m.get("success_requests"),
                    "failed_requests": m.get("failed_requests"),
                    "metrics": m,
                })
        pts.sort(key=lambda d: (d["concurrency"], d["dataset"]))
        return pts
