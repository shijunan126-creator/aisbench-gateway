# AISBench 网关（维护者文档）

把 AISBench 那套「手写 mmengine 配置 + 拼一长串 CLI 参数 + 去 outputs 里翻 JSON」
包成一个网页表单，并让多次测试结果可以对比。

面向**使用者的说明**在 [`README.customer.md`](README.customer.md)，那份会被打进交付包。
本文是给改代码的人看的。

```
宿主机                                   容器 aisbench-gateway
├── gw/  static/  tools/  config.ini  ──► /gateway   （只读挂载）
├── data/                             ──► /work      （读写挂载）
│    ├── configs/<job_id>/main.py         网关生成的 mmengine 配置
│    │                    warmup.py       前缀预热的配置（仅合成数据集）
│    ├── outputs/<job_id>/                AISBench 产物
│    ├── datasets/                        AIS_BENCH_DATASETS_CACHE
│    └── models/                          用户放 tokenizer 的地方
└── *.sh                                  纯 bash + docker 的运维脚本

容器主进程就是网关本身：python3 -m gw.main（--network host 暴露 8080）
```

---

## 一、三条硬约束

改代码前务必先理解，它们决定了整个工程的样子。

### 1. 网关跑在容器里，且零第三方依赖

客户机器**只需要 Docker**，不需要 python3。所以：

- 网关卡里所有 Python 代码**只能用标准库**（`gw/main.py` 是手写 `http.server`，
  `gw/settings.py` 用 `configparser` 而不是 PyYAML）
- 所有脚本（`*.sh`）**不得调用宿主机 python**，`config.ini` 的值用 awk 解析
  （`lib.sh` 里的 `ini_get` / `ini_set`）

用 `python3 -S -c "import gw.main"` 可以验证没引入第三方依赖。

### 2. 容器的工作目录是只读的，任务子进程必须显式指定 cwd

容器的工作目录设成 `/gateway`（为了让 `python3 -m gw.main` 能解析模块），
而它是**只读挂载**。但 aisbench 的 `LocalRunner` 会创建**相对路径**的 `tmp/`
（`runners/local.py::_launch` 里的 `mmengine.mkdir_or_exist('tmp/')`），
不指定 cwd 就会：

```
OSError: [Errno 30] Read-only file system: 'tmp/'
```

然后进程卡住不退出。所以 `gw/runner.py` 里 `Popen` **必须**带 `cwd=self.s.data_dir`。

### 3. 网关是容器 PID 1，必须自己装信号处理器

Linux 内核对 PID 1 会把「默认动作为终止」的信号（含 SIGTERM）**直接忽略**，
除非进程自己装了处理器。Python 默认不处理 SIGTERM，不装的话 `docker stop`
会干等 10 秒然后 SIGKILL 硬杀，正在跑的任务被硬中断。

`gw/main.py::_install_signal_handlers` 就是干这个的。装了之后 `docker stop` 秒级返回。

---

## 二、改 aisbench 相关代码前必读的坑

以下每条都是实测踩出来的，`catalog.py` / `confgen.py` / `results.py` 里有对应处理。

| 现象 | 原因 |
|---|---|
| `url` 填了 `/v1` 变成 `/v1/v1/completions` | aisbench 内部用 `urljoin(base, "v1/chat/completions")`。网关会剥掉结尾 `/v1` |
| `synthetic dataset miss required param` | 随机数据集和 sharegpt **必须**有本地 tokenizer 路径，镜像里一个都不带 |
| `BaseAPIModel.generate() missing 'output'` | `infer` 块的 task 类型选错了。服务化模型必须用 `OpenICLApiInferTask`。**网关故意不生成 `infer` 块**，交给 CLI 按 `attr="service"` 自己选 |
| `No module named ...runners.local_api` | 镜像里 `configs/api_examples/*.py` 是**过时的**，别照抄。当前 runner 在 `ais_bench.benchmark.runners.local` |
| `--num-prompts` 没生效 | 它与配置里的 `reader_cfg['test_range']` 互斥，后者存在时前者被静默忽略。网关只用 CLI 一种机制 |
| 数据下了但跑不了 | 各数据集压缩包内部结构不一致。`piqa.zip` 解出 `piqa/` 但配置要 `physicaliqa-train-dev`；`aime.zip` 解出的是裸文件。逐条声明 `ready_path` 和 `extract_into` |
| `ceval` 按 README 装不上 | README 让用 modelscope 的包，但那解出来是顶层 `dev/val/test/`；OSS 的 `ceval.zip` 才解出配置要的 `ceval/formal_ceval/`。**以压缩包实际结构为准，别信 README** |
| 任务失败只说「退出码 1」 | aisbench 报的错常常跟真实原因无关：**模型服务连不上**时它抛的是 `different structure of perf data`；**评估失败时退出码还是 0**（HumanEval 被截断会抛 `Some problems are not attempted`）。`results.find_failure_reason` 会从日志里把真实原因挖出来 |
| 配置里调个函数就崩 | mmengine 的 `Config.fromfile` 用惰性导入，被 import 的名字会包成 `LazyObject`，在配置顶层调用它直接抛 `RuntimeError`（`lazy.py:103`）。详见下节 |
| CustomDataset 的 prompt 里混进了 `Answer: none` | `make_custom_dataset_config` 默认模板是 `"Question: {question}\nAnswer: {answer}"`，**会把 answer 拼进 prompt**。必须显式传 `template` |

### 在配置里合成数据集（`prefix_gen`）

`gw/dataset_gen.py` 按指定长度和前缀比例从 GSM8K 合成数据集，配置在
`confgen._custom_dataset_config` 里以**字面量**写出来。三点必须知道：

1. **配置里不能有函数调用。** mmengine 解析配置时会把 import 进来的名字包成
   `LazyObject`，调用即抛 `RuntimeError`。所以不能在配置里写
   `datasets = [make_custom_dataset_config({...})]`，只能在网关进程里先算好、
   再用 `_py()` 序列化成字面量。好在它的返回值全是可序列化的字面量
   （`type` 已经是全限定字符串），这条路才走得通。

2. **不能直接用 `make_custom_dataset_config`。** 它内部的 `parse_example_dataset`
   会真的 open 数据集文件读第一行，而提交任务时我们只做配置预校验，
   那时数据集还没生成，文件不存在。所以改调它的下一层 `make_qa_gen_config`，
   meta 由我们自己填 —— 生成的数据集的 schema 是我们自己定的（`question` 正文、
   `answer` 占位、没有 A/B 选项），推出来的值必然一致。

3. **必须显式传 `template="{question}"`。** 默认模板会把 `answer` 拼进 prompt，
   每条请求凭空多出十几个 token，长度就不是用户指定的那个了。

为什么只支持 GSM8K：参考项目 `aisbench_auto_tools_prefix` 也只支持它。
做通用的代价在各数据集**文本字段名和目录结构都不统一**（mmlu/ceval 是无表头 CSV、
math 的 json 是 dict、humaneval 用 `prompt`、mbpp 用 `text`、hellaswag 用 `query`），
要写字段探测加逐数据集特例，收益不成比例。日后要扩，加一张字段映射表即可，
`dataset_gen` 的生成算法本身不用动。

预热阶段是**第二次 aisbench 调用**，配置写在 `<job>/warmup.py`、产物落在
`outputs/<job>/warmup/`。这么安排是因为 `results.find_runs` 只认 `\d{8}_\d{6}`
格式的子目录，预热产物不会被误当成正式结果。

### prefix cache 命中率（`gw/metrics.py`）

口径抄自同一个参考项目的 `cal_prefix_hit_rate.py`：**压测前后各取一次
`/metrics` 快照，用增量算**。

```
命中率 = (hits_after - hits_before) / (queries_after - queries_before)
```

几个必须守住的点：

1. **必须用增量。** `vllm:prefix_cache_{queries,hits}_total` 是进程启动以来的
   累计值，直接读它包含此前所有请求（上次测试、别人的请求……），拿来当本次命中率是错的。

2. **按 `(端点, engine)` 分开算再汇总。** 一个 pod 下可能有多个 DP 域，
   各自的 KV cache 独立。合在一起看不出"某个域根本没吃到前缀"，
   而那正是预热没做对时的典型症状。

3. **快照要取在 aisbench 进程之外，且后快照必须在进程退出之后。**
   早取会漏掉最后一批还在途的请求。取在进程内会挤占压测窗口。

4. **快照只取在正式阶段。** 预热阶段不取：那一轮就是在灌前缀，命中率没有意义。
   顺带一提，正因为预热在正式阶段之前，正式阶段的第一条请求就能命中 ——
   不预热的话每个域的头几条会拉低平均值。

5. **取不到不能让任务失败。** 服务可能不是 vLLM、没开 `/metrics`、或者地址填错。
   这些都只记进 `skipped`/`note`，任务照常成功，结果里**不会**混入 0 这种假数据
   （`summary_metrics` 在没有真实增量时返回空字典，对比图上就是缺一根柱，而不是 0%）。

6. **Δqueries 为 0 是个诊断信号，不是"命中率 0%"**，要单独提示：
   说明请求压根没打到你查的那个服务。

查哪些端点：默认用被测服务自己（`base_url` 的 host:port），PD 混部够用；
PD 分离时在 `config.ini` 的 `[metrics] pods` 里配各个 P 节点。

`tools/mock_openai_server.py` 也带了个 `/metrics`，否则这条链路没法自测。
它的命中率是**按字符块粗略模拟**的（没有 tokenizer），只能用来验证
采集→增量→展示这条链路通不通，数值不代表任何真实引擎的行为。

### 任务认领必须是原子的

`store.claim_queued()` 把「选队首」和「标记 running」放在同一次加锁事务里，
用带 `status='queued'` 条件的 UPDATE + `rowcount` 判断有没有抢到。

**不要改回「先 `next_queued()` 看一眼，再在 `_execute` 里标 running」**：
那中间有窗口，并发度 >1 时两个 worker 会抢到同一条任务并各跑一遍 ——
两个 aisbench 同时压同一个服务，结果还会入库两次，对比页里凭空多出一条
"看起来像另一次运行"的记录。把 `max_concurrent_jobs` 设成 2 实测复现过。

`next_queued()` 现在只用于展示/诊断，**不要拿它去驱动执行**。

顺带一提：`max_concurrent_jobs > 1` 时启动会打一条警告。任务不会被重复执行了，
但**并发跑压测**会让两个任务抢同一个模型服务，把吞吐数据互相污染。
压测应当保持 1，只有精度测试适合调大。

### 备注与「标签 / 备注」两个字段

`label` 是系统按数据集和参数自动生成的（合成数据集还会带上 `prefix75-in4096`
这样的指纹），`note` 是用户自己写的。两者都留着，展示时**备注优先**：

- 任务列表、对比页的图表都显示 `note || label`，自动标签降级成小字
- **扫描的备注只存在父任务上**，子任务读取时才回退过去（`store.resolve_notes`）。
  不要在下发时给子任务各写一份 —— 父任务事后改备注时子任务不会跟着变，
  两边就不一致了。（踩过一次，改成了读时兜底。）
- 回退要覆盖 **Sweep 父任务里嵌套的 `children`**：那是另一次查询出来的对象副本，
  跟顶层列表里的同名任务不是同一个 dict，只遍历顶层会漏掉一半界面
- 对比接口给扫描子任务的 label 会**接上「并发 N」**，否则同一组扫描的几条在图上同名，
  而"几档并发之间怎么变"正是扫描要看的
- 加列走 `store._ADDED_COLUMNS` 的就地迁移。客户库里已经有数据，**不能指望删库重来**

### 产物解析的两个坑

`gw/results.py` 已处理，但改的时候要知道：

- 带单位的指标在 JSON/CSV 里**是字符串**，如 `"404.83 ms"`、`"209.96 token/s"`，解析要剥单位
- **端到端指标在 `.json`，逐请求分位指标在 `.csv`**，两者都要读
- JSON 里的键叫 **`Total Generated Tokens`**，不是 `Total Output Tokens`
  —— 单位映射表里写的是后者，已过时，那个键根本不会出现

---

## 三、本地开发

```bash
./run.sh       # 前台运行，实时看日志（Ctrl-C 停）
./start.sh     # 后台运行
./stop.sh      # 停止
./status.sh    # 状态与诊断
docker logs -f aisbench-gateway   # 看实时日志
```

改前端（`static/`）不用重启，静态文件每次请求现读。
改 `gw/` 下的 Python 需要 `./stop.sh && ./start.sh`（重建容器，约 2 秒）。

### 自测

```bash
./tools/run_mock_server.sh          # 假模型服务，跑在容器里
./tools/run_mock_server.sh --stop
```

它的延迟是**已知**的（TTFT 50ms、每 token 5ms），所以压出来的数字可以交叉验算：
并发 C、输出 N token 时，单请求 E2EL ≈ TTFT + N×TPOT。

注意它返回随机词，**精度得分没有意义**。重启网关容器后它也会一起退出。

### 端到端验证清单

改动后至少跑一遍：

1. `python3 -S -c "import gw.main"` —— 确认没引入第三方依赖
2. `./status.sh` 正常，页面右上角显示「服务正常」
3. 用假服务跑一次性能测试，确认端到端指标和分位指标**都不为空**
4. 跑一次精度测试（gsm8k 子集即可），确认得分入库
5. 提交并发扫描（如 `1,2,4`），确认拆子任务、串行执行、曲线出点
6. 任务在跑时 `docker stop`，确认**秒级返回**且没有残留的 ais_bench 进程
7. 把宿主机 `python3` 屏蔽掉（PATH 里放个假的 `python3` 桩），
   确认 `./install.sh` / `./start.sh` / `./status.sh` 仍能正常工作
8. 跑一次「GSM8K 前缀数据集」（勾上预热），确认：
   - 日志里能看到「预热前缀」和「正式测试」两段
   - 详情页「实测长度」与设定值相差在 2% 以内
   - **结果只入库 1 条**（预热的产物在 `outputs/<job>/warmup/`，不算数）
   - 同参数再提交一次，日志里出现「复用已生成的数据集」
9. 勾上「采集实际命中率」，对着假服务跑一次：假服务的 prefix cache 是
   按块模拟的，**测出来的命中率应当接近设定的前缀比例**（实测 50% 设定
   得到 50.8%）。再把 `[metrics] pods` 指到一个关闭的端口，确认任务
   仍然成功、日志里说明跳过原因、结果里不出现假的 0%

---

## 四、新增数据集

1. 从镜像里找出配置：
   ```bash
   docker exec aisbench-gateway ls /benchmark/ais_bench/benchmark/configs/datasets/<family>/
   docker exec aisbench-gateway grep -rhoE "path=.ais_bench/datasets/[a-zA-Z0-9_./-]*" \
     /benchmark/ais_bench/benchmark/configs/datasets/<family>/*.py | sort -u
   ```
2. **下载压缩包，实际解压看目录结构**——不要相信 README 里的说明
   （可以只读 zip 中央目录，不用整包下载）
3. 在 `gw/catalog.py` 里加一条，写清 `ready_path`（配置期望的路径）、
   `extract_into`（解压到哪）、`url`、变体列表
4. 在页面上点下载，确认文件落到 `ready_path` 上，再跑一次确认能出结果

---

## 五、打包交付

```bash
./make-release.sh                          # 默认版本、默认数据集
./make-release.sh --version 1.1            # 指定版本
./make-release.sh --split 1900M            # 分片（U 盘/FAT32 传输）
./make-release.sh --datasets gsm8k,ceval   # 只带指定数据集
./make-release.sh --no-image               # 只打代码（镜像让客户自行 pull）
```

产出 `dist/aisbench-gateway-<版本>.tar.gz`，客户解压后跑 `./install.sh`。

### 镜像导出必须校验

**本机那份 aisbench 镜像的层元数据可能损坏**，`docker save` 会**静默产出不完整的包**
（退出码 0、大小看着正常，但包里引用了不存在的 blob）。本机因为已有那些层看不出来，
到客户的全新机器上才会报 `failed to extract layer ... content digest ... not found`。

所以 `make-release.sh` 导出后做两道校验，任一不通过就换方案（`save → commit → pull`）：

1. 结构校验：manifest 引用的每个 blob 都在包内（纯文件检查，安全）
2. 实际载入并跑 `ais_bench --help`

`docker commit`（从全新容器）能生成元数据完好的镜像，是最可靠的兜底。

### 发出前务必做干净环境验证

```bash
mkdir -p /tmp/cleanroom && cd /tmp/cleanroom
tar xzf /path/to/aisbench-gateway-1.0.tar.gz
cd aisbench-gateway-1.0
./install.sh --yes
```

确认导入镜像、铺数据集、起容器、起网关、页面可访问全部成功，并**提交一次任务**。

---

## 六、镜像架构（曾经的错误前提，务必先看这段）

**「本机 x86 靠 QEMU 模拟 arm64 镜像」这个说法是错的**，之前的文档和
`install.sh` 都写错了。实测：

```
docker image inspect <image> --format '{{.Architecture}}'   → amd64
docker exec <容器> uname -m                                  → x86_64
head -c 20 /bin/bash | od                                    → EM_X86_64 (3e 00)
```

即：**这台 x86 机器上跑的是原生 amd64，没有任何模拟**。据此推断的
「QEMU 导致死锁」也就不成立（死锁症状本身是真的，原因另说，见下）。

### 交付包必须裁成单平台

`docker save` 一个多平台 tag 会产出「混合」tar：

- `index.json` → manifest list，含 amd64 + arm64。**containerd 镜像存储**读它，按平台挑
- `manifest.json` → 只写**打包机那一个平台**。**经典/overlay2 存储**的 `docker load` 只读它

于是同一个包在不同客户机上 load 出不同架构，**取决于对方的 Docker 版本**。
在 x86 开发机上打出来的包，拿到 arm64 客户机、对方又是经典存储时，
load 出来是 amd64 → `docker run` 平台不匹配 → 网关起不来，而打包机自测一切"正常"。

所以 `make-release.sh` 现在会：

1. `--platform`（默认 `linux/arm64`，即交付目标）
2. 导出后调 `to_single_platform` 把 tar 裁成单平台（两条 load 路径结果一致，
   与 Docker 版本无关），顺带把包体从 ~1.4G 降到 ~700M
3. `commit` 兜底方案在目标平台 ≠ 打包机架构时**直接跳过** —— 它只会生成
   打包机架构的镜像
4. 校验时把架构报出来
5. 把架构写进包级 `manifest.json` 的 `requires.arch`，`install.sh` 拿它和本机比，
   不一致就明确报错并退出（exit 1）

裁的时候**必须保留 `index.json` 里的 annotations**：containerd 路径下镜像名/tag
靠 `io.containerd.image.name` 恢复，丢了 `docker load` 只会打印 "Loaded image ID"，
`install.sh` 按 tag 就找不到镜像。

### 偶发死锁（原因仍未确定）

- 卡点固定在 `Launch TasksMonitor` 之后（fork 工作子进程那一步），日志再无输出
- 症状是 HTTP 请求其实已发完、连接进了 TIME_WAIT，但进程 CPU 时间不再增长且不退出
- 同样的配置有时跑通有时卡死，不可预测；内存和负载都很宽裕，不是资源问题

**How to apply:** 在本机验证时用极小样本量（`RequestCount=2~6`），
不要把偶发卡死当成回归；但也**不要归给 QEMU** —— 这里根本没有模拟。
`config.ini` 的 `runner.stall_timeout` 看门狗仍然需要。

---

## 七、HTTP 接口

```
GET    /api/config                  接口类型、数据集、tokenizer 候选、自检状态
GET    /api/health
POST   /api/selfcheck               重新做一次运行环境自检

GET    /api/datasets                数据集就绪状态与下载进度
POST   /api/datasets/{family}/download

POST   /api/jobs                    提交（给了 concurrency_list 则自动拆扫描）
GET    /api/jobs                    列表
GET    /api/jobs/{id}               详情（含结果与扫描序列）
GET    /api/jobs/{id}/log?offset=N  增量拉日志
POST   /api/jobs/{id}/cancel
POST   /api/jobs/{id}/note          写备注（空串即清除）
POST   /api/jobs/{id}/reparse       按当前逻辑重新解析磁盘产物（不必重跑）
DELETE /api/jobs/{id}

GET    /api/results                 结果列表
GET    /api/results/compare?job_ids=a,b,c

GET    /api/artifacts/{path}        取产物文件（原生 plot.html、逐请求 jsonl 等）
```

---

## 八、文件说明

| 文件 | 作用 |
|---|---|
| `gw/catalog.py` | 接口类型与精选数据集（每条都实测核验过） |
| `gw/dataset_gen.py` | 按指定长度 + 前缀比例从 GSM8K 合成数据集（见第二节末尾） |
| `gw/metrics.py` | 从 `/metrics` 读 prefix cache 命中率（增量算法，见第二节末尾） |
| `gw/confgen.py` | 表单 → mmengine 配置（核心） |
| `gw/runner.py` | 任务队列、串行执行、日志落盘、看门狗、进程组管理 |
| `gw/results.py` | 产物解析归一化、失败原因提取 |
| `gw/datasets_mgr.py` | 数据集就绪检测与下载 |
| `gw/runtime.py` | 容器内运行环境自检（**以前叫 container.py**，那时用 docker API 管容器） |
| `gw/store.py` | SQLite。加列走 `_ADDED_COLUMNS` 就地迁移，**不要指望用户删库重来** |
| `gw/main.py` | HTTP 路由（标准库）+ 信号处理 |
| `gw/settings.py` | 配置（configparser） |
| `lib.sh` | 各脚本共用的函数（ini 解析、docker 检查、健康检查） |
| `static/app.js` | 前端全部交互与图表 |

### 前端约定

图表遵循一套可视化规范，改的时候注意：

- **绝不使用双轴**。吞吐（req/s、token/s）与时延（ms）量纲不同，必须拆成不同的图
- 每个图都配「表格」视图（部分分类色对比度偏低，规范要求有此补偿）
- 颜色跟随「运行记录」这个实体，槽位在首次出现时分配并固定 —— 勾选变化不能重新着色
- 最多同时对比 8 条（分类色只有 8 个槽位），第 9 条会被拦下
