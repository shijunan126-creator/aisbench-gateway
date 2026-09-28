"""按「指定长度 + prefix 复用比例」合成 GSM8K 数据集。

解决的问题：真实数据集的 prompt 长度是固定的，做不了长上下文压测；
而 prefix cache 命中率更是需要「前缀相同、后续不同」这种刻意构造的数据。

算法来自 [aisbench_auto_tools_prefix](https://github.com/rayn-zzz/aisbench_auto_tools_prefix)
的 `generate_dataset.py`：::

    prefix_len = input_len × prefix_ratio
    前缀池     = prefix_num 条互不相同的文本，各自填充/截断到 prefix_len
    后缀       = 每条一条文本，填充/截断到 (input_len - prefix_len - 3)
    每条数据   = 前缀池[i % prefix_num] + 3个唯一随机token(seed) + 后缀[i]

中间那 3 个 token 是关键：它让每条请求的内容互不相同（不会被服务端的
请求去重或 batch 合并吃掉），但前缀完全一致，于是 prefix cache 恰好命中
前缀部分、在中间失效。这样测出来的才是「指定命中率下的性能」。

**语料只取 GSM8K**。参考项目也只支持 GSM8K（它把生成的 jsonl 软链成
`test.jsonl` 塞进 gsm8k 数据目录）。做通用的代价在于各数据集的文本字段名和
目录结构都不统一 —— mmlu/ceval 是无表头 CSV、math 的 json 是 dict、
humaneval 用 `prompt`、mbpp 用 `text`、hellaswag 用 `query` —— 要写字段探测和
逐数据集特例，收益不成比例。日后要扩数据集，加一张字段映射表即可，
下面的生成算法本身不用动。

两个依赖只在函数内导入：
- `transformers`（重，且只在真正生成时才需要）
- 无 `torch` —— 参考项目里 `torch.manual_seed` 是多余的，取 token id 用的是
  `random.randint`，torch 的随机流根本没参与，所以这里不引入。
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("gw.dataset_gen")

# 语料位置，相对 AIS_BENCH_DATASETS_CACHE（网关设为 <data>/datasets）
GSM8K_REL = "ais_bench/datasets/gsm8k/test.jsonl"

# 前缀和后缀之间插入的唯一 token 个数。见模块开头的说明，不要改成 0。
UNIQUE_TOKENS = 3

# 生成文件名（放在每个 spec 自己的目录里，所以不需要再拼参数）
DATA_NAME = "data.jsonl"
PREFIX_NAME = "prefix.jsonl"
META_NAME = "meta.json"


class GenError(RuntimeError):
    """生成失败，消息直接展示给用户。"""


class GenCancelled(Exception):
    """用户在生成途中取消了任务。

    生成是纯 Python 的 CPU 活，没法从外面直接掐断，只能在每行的循环里
    协作式地检查。不检查的话，取消一个 128k × 大条数的任务要等几分钟才有反应，
    而那正是用户最想取消的时刻。
    """


# ---------------------------------------------------------------- 参数

@dataclass
class Spec:
    """一次生成的全部输入参数。"""

    input_len: int = 2048
    num: int = 100
    prefix_ratio: float = 0.0
    prefix_num: int = 1
    dp: int = 1
    seed: int = 1

    # 变长模式。两组互斥：给了 mean/std 用高斯，给了 min/max 用均匀，
    # 都不给就是定长 input_len。
    length_mean: Optional[int] = None
    length_std: Optional[float] = None
    length_min: Optional[int] = None
    length_max: Optional[int] = None

    @property
    def variable_length(self) -> bool:
        return (
            (self.length_mean is not None and self.length_std is not None)
            or (self.length_min is not None and self.length_max is not None)
        )

    @property
    def uses_prefix(self) -> bool:
        return self.prefix_ratio > 0

    def validate(self) -> None:
        """把放不下的参数组合拦在这里。

        挂在 Spec 上而不是只挂在 `spec_from_params` 上：后者只是 UI 参数的
        一层适配，直接构造 Spec 的调用方（脚本、测试）也要受同样的约束，
        否则会生成出长度不对的数据集，而用户只会看到「实测长度和填的不一样」。
        """
        if self.input_len < 1:
            raise GenError("输入长度必须 ≥ 1")
        if self.num < 1:
            raise GenError("数据条数必须 ≥ 1")
        if self.prefix_num < 1:
            raise GenError("前缀个数必须 ≥ 1")
        if self.dp < 1:
            raise GenError("DP 域数必须 ≥ 1")
        if not (0.0 <= self.prefix_ratio <= 1.0):
            raise GenError("前缀比例必须在 0~1 之间")

        # 比例 >= 1 时整条 prompt 就是前缀，没有后缀，不需要留空间
        if not self.uses_prefix or self.prefix_ratio >= 1.0:
            return

        need = 2 + UNIQUE_TOKENS      # 前缀至少 1 token + 中间 3 个唯一 token + 后缀至少 1 token
        if self.variable_length:
            lo = self.length_min if self.length_min is not None else (self.length_mean or 1)
            if lo < need:
                raise GenError(
                    f"最短长度 {lo} 太小：前缀要占一部分、中间还要插 "
                    f"{UNIQUE_TOKENS} 个唯一 token，至少需要 {need}。请调大长度下限或均值。"
                )
        else:
            plen = int(self.input_len * self.prefix_ratio)
            if plen < 1:
                raise GenError(
                    f"输入长度 {self.input_len} 配前缀比例 {self.prefix_ratio:.2%} "
                    "算出来不足 1 个 token，构造不出前缀。请调大输入长度或前缀比例。"
                )
            if plen + UNIQUE_TOKENS > self.input_len:
                raise GenError(
                    f"输入长度 {self.input_len} 放不下：前缀 {plen} + 唯一 token "
                    f"{UNIQUE_TOKENS} 已经超出。请调大输入长度或调小前缀比例。"
                )

    def cache_key(self, tokenizer_path: str) -> str:
        """参数指纹。tokenizer 也参与 —— 换 tokenizer 等于换了一套 token 切分。"""
        payload = json.dumps(
            {**asdict(self), "_tok": str(tokenizer_path)}, sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

    def summary(self) -> str:
        if self.variable_length:
            if self.length_mean is not None and self.length_std is not None:
                ln = f"高斯 {self.length_mean}±{self.length_std}"
                if self.length_min is not None and self.length_max is not None:
                    ln += f"（截断到 {self.length_min}~{self.length_max}）"
            else:
                ln = f"均匀 {self.length_min}~{self.length_max}"
        else:
            ln = f"定长 {self.input_len}"
        s = f"{ln} · {self.num} 条 · 种子 {self.seed}"
        if self.uses_prefix:
            s += f" · 前缀 {self.prefix_ratio:.0%} × {self.prefix_num} 种"
        return s


def parse_prefix_ratio(raw: Any) -> float:
    """`50%` / `0.5` / `0.500` 都能解析成 0.5。

    抄参考项目的 `parse_prefix_ratio` —— 两种写法在它的文档里都出现过，
    用户按文档填哪个都得认。
    """
    s = str(raw).strip()
    if not s:
        return 0.0
    try:
        v = float(s[:-1]) / 100.0 if s.endswith("%") else float(s)
    except ValueError:
        raise GenError(f"前缀比例无法解析：{raw!r}（支持 50% 或 0.5 两种写法）")
    if not (0.0 <= v <= 1.0):
        raise GenError(f"前缀比例必须在 0~1 之间或 0%~100%，当前是 {raw!r}")
    return v


def _opt_int(v: Any) -> Optional[int]:
    if v is None or str(v).strip() == "":
        return None
    return int(float(v))


def _opt_float(v: Any) -> Optional[float]:
    if v is None or str(v).strip() == "":
        return None
    return float(v)


def spec_from_params(p: Dict[str, Any]) -> Spec:
    """从 UI 参数构造 Spec，并把非法值在这里拦下（报错信息比容器里的堆栈友好）。"""
    input_len = int(p.get("gen_input_len") or p.get("input_len") or 2048)
    if input_len < 1:
        raise GenError("输入长度必须 ≥ 1")

    num = int(p.get("gen_num") or p.get("num_prompts") or 100)
    if num < 1:
        raise GenError("数据条数必须 ≥ 1")

    prefix_num = max(1, int(p.get("gen_prefix_num") or 1))
    dp = max(1, int(p.get("gen_dp") or 1))

    spec = Spec(
        input_len=input_len,
        num=num,
        prefix_ratio=parse_prefix_ratio(p.get("gen_prefix_ratio") or 0),
        prefix_num=prefix_num,
        dp=dp,
        seed=int(p.get("gen_seed") or 1),
    )

    # 长度分布：三种模式由前端单选决定，这里只取对应的一组，避免两组都填时含糊。
    mode = (p.get("gen_len_mode") or "fixed").strip()
    if mode == "gauss":
        spec.length_mean = _opt_int(p.get("gen_len_mean"))
        spec.length_std = _opt_float(p.get("gen_len_std"))
        spec.length_min = _opt_int(p.get("gen_len_min"))
        spec.length_max = _opt_int(p.get("gen_len_max"))
        if spec.length_mean is None or spec.length_std is None:
            raise GenError("高斯分布需要同时填「长度均值」和「标准差」")
    elif mode == "uniform":
        spec.length_min = _opt_int(p.get("gen_len_min"))
        spec.length_max = _opt_int(p.get("gen_len_max"))
        if spec.length_min is None or spec.length_max is None:
            raise GenError("均匀分布需要同时填「长度下限」和「长度上限」")
        # 定长 input_len 在变长模式下不参与采样，但命名和兜底还得有个值
        spec.input_len = max(spec.length_min, spec.length_max)

    spec.validate()
    return spec


# ---------------------------------------------------------------- 产物

@dataclass
class GenResult:
    dataset_path: Path
    prefix_path: Optional[Path]
    dir: Path
    cached: bool = False
    stats: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- 基础操作

def _each(n: int, should_stop: Optional[Callable[[], bool]] = None):
    """产出 0..n-1，每次前问一句「还要继续吗」。

    逐行生成的地方都用它来数循环次数，这样取消能及时生效（见 GenCancelled）。
    **必须产出下标**：有几处拼接要用它去取前缀池和后缀池的对应元素，
    只产出 None 的话那些地方会直接 TypeError。
    """
    for i in range(n):
        if should_stop is not None and should_stop():
            raise GenCancelled()
        yield i


def _resize(ids: List[int], target: int) -> List[int]:
    """把 token 序列调整到 target 长度：超了截断，不够就整段重复。"""
    if target <= 0:
        return []
    if len(ids) >= target:
        return ids[:target]
    reps = (target + len(ids) - 1) // len(ids)
    return (ids * reps)[:target]


def _fit(tok: Any, text: str, target: int) -> str:
    """把 text 调成大约 target 个 token 的文本。

    这是「指定长度」的核心。**必然有偏差**：tokenize → decode 是有损往返
    （生僻 token 的字节序列 decode 再 encode 会变），所以 decode 回来长度可能变，
    这里再修一次。修完仍有 ±1~2% 的残差，参考项目也一样，UI 上要如实说明。
    """
    ids = tok.encode(text, add_special_tokens=False)
    if not ids:
        return ""
    out = tok.decode(_resize(ids, target), skip_special_tokens=True)

    back = tok.encode(out, add_special_tokens=False)
    if len(back) != target and back:
        out = tok.decode(_resize(back, target), skip_special_tokens=True)
    return out


def _sample_length(rng: random.Random, spec: Spec) -> int:
    """按分布采样一条数据的实际输入长度（抄参考项目 `sample_target_length`）。

    没配分布就是定长 input_len。
    """
    lo = 1
    hi: Optional[int] = None
    if spec.length_min is not None and spec.length_max is not None:
        lo, hi = int(spec.length_min), int(spec.length_max)
        if lo > hi:
            lo, hi = hi, lo
        lo = max(1, lo)
        hi = max(1, hi)

    if spec.length_mean is not None and spec.length_std is not None:
        mu = max(1, int(spec.length_mean))
        sigma = max(0.0, float(spec.length_std))
        v = mu if sigma == 0 else int(round(rng.gauss(mu, sigma)))
        if hi is not None:
            v = min(v, hi)
        return max(1, max(lo, v))

    if hi is not None:
        return rng.randint(lo, hi)

    return max(1, int(spec.input_len))


class _Picker:
    """随机取语料，尽量不重复。

    参考项目把「已用过的 id」落在 `picked_ids.txt`，用尽后直接报
    「生成数据集失败，请清空picked ids」（它 FAQ 第一条），用户得手工删文件才能恢复。
    这里改成**用尽即自动重置并复用**：效果一样（数据不会因此变差，因为长度拟合
    本来就靠重复填充），但不会让人卡在一个莫名其妙的失败上。

    唯一要求严格不重复的是前缀池 —— 前缀种类数就是这个池子的大小，
    池子里出现重复等于变相减少了前缀种类，所以 `allow_reuse=False`。
    """

    def __init__(self, texts: List[str], rng: random.Random, allow_reuse: bool = True):
        self._texts = texts
        self._rng = rng
        self._allow_reuse = allow_reuse
        self._used: set[int] = set()
        self.reset_count = 0

    def take(self) -> str:
        if len(self._used) >= len(self._texts):
            if not self._allow_reuse:
                raise GenError(
                    f"语料不够：需要 {len(self._texts) + 1} 条互不相同的文本，"
                    f"但 GSM8K 只有 {len(self._texts)} 条。请调小「前缀个数」。"
                )
            self._used.clear()
            self.reset_count += 1
        while True:
            i = self._rng.randrange(len(self._texts))
            if i not in self._used:
                self._used.add(i)
                return self._texts[i]


def _unique_token_texts(tok: Any, seed: int, n: int, rows: int) -> List[str]:
    """生成 rows 行、每行 n 个互不相同的随机 token（decode 成文本）。

    每行用 seed+行号 起一个独立的随机流，保证行与行之间不重复 ——
    这正是「每条请求内容不同」的来源。
    """
    vocab = len(tok)
    if n > vocab:
        raise GenError(f"每行要 {n} 个唯一 token，超过词表大小 {vocab}")

    out: List[str] = []
    for row in range(rows):
        rng = random.Random(seed + row)
        seen: set[int] = set()
        parts: List[str] = []
        guard = 0
        limit = n * 50      # 词表很小时随机撞车的概率不低，给足重试次数
        while len(parts) < n and guard < limit:
            tid = rng.randrange(vocab)
            guard += 1
            if tid in seen:
                continue
            try:
                text = tok.decode([tid])
            except Exception:  # noqa: BLE001  个别 token 解不出来是正常的
                continue
            seen.add(tid)
            parts.append(text)
        if len(parts) < n:
            log.warning("第 %d 行只取到 %d/%d 个唯一 token", row + 1, len(parts), n)
        out.append("".join(parts))
    return out


# ---------------------------------------------------------------- 语料

def load_corpus(datasets_dir: Path) -> List[str]:
    """读 GSM8K 的 `question` 字段作为语料。

    GSM8K 是内置数据集（交付包里预置），所以正常情况下必然存在；
    不存在时给一句能直接照做的提示，而不是抛 FileNotFoundError 堆栈。
    """
    path = Path(datasets_dir) / GSM8K_REL
    if not path.exists():
        raise GenError(
            f"找不到 GSM8K 语料：{path}。"
            "该数据集随交付包预置，若被删可从「数据集」页重新下载，"
            "或手工把 gsm8k.zip 解到数据目录的 ais_bench/datasets/ 下。"
        )

    texts: List[str] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            q = rec.get("question") if isinstance(rec, dict) else None
            if isinstance(q, str) and q.strip():
                texts.append(q.strip())

    if not texts:
        raise GenError(f"GSM8K 语料为空或格式不符（期望每行一个 question 字段）：{path}")
    return texts


# ---------------------------------------------------------------- 主流程

def _write_rows(path: Path, rows: List[str]) -> None:
    """写成 aisbench CustomDataset 认的 jsonl。

    `answer` 是占位符：这份数据集只用于性能测试，精度得分没有意义
    （confgen 会拦掉精度模式）。格式与参考项目的 `write_data` 一致。
    """
    with path.open("w", encoding="utf-8") as f:
        for text in rows:
            f.write(json.dumps({"question": text, "answer": "none"}, ensure_ascii=False))
            f.write("\n")


def generate(
    spec: Spec,
    tokenizer_path: str,
    datasets_dir: Path,
    base_dir: Path,
    progress: Optional[Callable[[str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> GenResult:
    """生成数据集。同样的参数重复调用会直接复用已有产物。

    `datasets_dir` 是 AIS_BENCH_DATASETS_CACHE（语料从这里读），
    `base_dir` 是产物落盘处（每个 spec 一个子目录）。
    """
    def say(msg: str) -> None:
        log.info("%s", msg)
        if progress:
            try:
                progress(msg)
            except Exception:  # noqa: BLE001  回调不该影响生成
                pass

    if not (tokenizer_path or "").strip():
        raise GenError("需要本地 tokenizer 路径才能生成数据集（用于把文本调整到指定 token 长度）")

    spec.validate()

    out_dir = Path(base_dir) / spec.cache_key(tokenizer_path)
    data_path = out_dir / DATA_NAME
    meta_path = out_dir / META_NAME

    # 幂等：同参数重复提交不重新生成。长上下文生成很慢（128k × 大条数要几分钟），
    # 扫描模式下一个参数被反复用到，不缓存会白白等很久。
    if data_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if int(meta.get("rows", -1)) == spec.num:
                say(f"复用已生成的数据集：{out_dir.name}（{spec.summary()}）")
                pre = out_dir / PREFIX_NAME
                return GenResult(
                    dataset_path=data_path,
                    prefix_path=pre if pre.exists() else None,
                    dir=out_dir, cached=True, stats=meta.get("stats") or {},
                )
        except (ValueError, OSError):
            log.warning("meta.json 损坏，重新生成 %s", out_dir)

    out_dir.mkdir(parents=True, exist_ok=True)

    # transformers 在这里才导入 —— 它是重依赖，而且只在真正生成时才需要，
    # 放在模块顶层会让网关启动变慢。
    try:
        from transformers import AutoTokenizer
    except ImportError as e:  # pragma: no cover
        raise GenError(f"容器内缺少 transformers，无法生成数据集：{e}")

    say(f"加载 tokenizer：{tokenizer_path}")
    try:
        tok = AutoTokenizer.from_pretrained(tokenizer_path, trust_remote_code=True)
    except Exception as e:  # noqa: BLE001
        raise GenError(
            f"tokenizer 加载失败：{tokenizer_path} —— {e}。"
            "注意这里要填**容器内**路径（真正读它的是容器里的网关进程）。"
        )

    corpus = load_corpus(datasets_dir)
    say(f"语料：GSM8K {len(corpus)} 条")

    rng = random.Random(spec.seed)
    stats: Dict[str, Any] = {"corpus_rows": len(corpus), "rows": spec.num}

    # 每条数据的实际长度（定长模式下就是 input_len）
    if spec.variable_length:
        real_lens = [_sample_length(rng, spec) for _ in range(spec.num)]
    else:
        real_lens = [spec.input_len] * spec.num
    stats["target_len_min"] = min(real_lens)
    stats["target_len_max"] = max(real_lens)
    stats["target_len_avg"] = round(sum(real_lens) / len(real_lens), 1)
    say(f"目标长度：{stats['target_len_min']}~{stats['target_len_max']}"
        f"（均值 {stats['target_len_avg']}），共 {spec.num} 条")

    corpus_picker = _Picker(corpus, rng, allow_reuse=True)
    prefix_path: Optional[Path] = None

    # ---------- 无前缀：纯粹的长度数据集 ----------
    if not spec.uses_prefix:
        say("生成正文（无前缀）…")
        longest = max(real_lens)
        rows = [_fit(tok, corpus_picker.take(), longest)
                for _ in _each(spec.num, should_stop)]
        if spec.variable_length:
            # 先按最大长度生成一批，再逐条截到各自的目标长度。
            # 比按长度分组各生成一次省事，代价是多 tokenize 一遍。
            rows = [_fit(tok, t, rl) for t, rl in zip(rows, real_lens)]
        _write_rows(data_path, rows)
        stats["prefix_ratio"] = 0.0

    # ---------- 变长（+ 可选前缀）----------
    elif spec.variable_length:
        # 前缀比例 >= 1 → 整条 prompt 就是前缀，没有后缀、也不插唯一 token
        full_prefix = spec.prefix_ratio >= 1.0
        common_lens = real_lens if full_prefix else [
            max(0, min(rl, int(round(rl * spec.prefix_ratio)))) for rl in real_lens
        ]
        # 前缀池按**最大**公共长度生成一次，逐条再截短
        pool_texts = _build_prefix_pool(
            tok, corpus, spec, rng, say, length=max(max(common_lens), 1),
            should_stop=should_stop,
        )
        prefix_path = _write_prefix_file(out_dir, pool_texts, spec)

        if full_prefix:
            uniq_n, max_suffix = 0, 0
        else:
            max_suffix = max(1, max(rl - cl - UNIQUE_TOKENS
                                    for rl, cl in zip(real_lens, common_lens)))
            say(f"生成后缀池（最长 {max_suffix} token）…")
            suffix_pool = [_fit(tok, corpus_picker.take(), max_suffix)
                           for _ in _each(spec.num, should_stop)]
            uniq = _unique_token_texts(tok, spec.seed, UNIQUE_TOKENS, spec.num)
            uniq_n = UNIQUE_TOKENS

        say("拼接数据集…")
        rows = []
        for i in _each(len(real_lens), should_stop):
            head = _fit(tok, pool_texts[i % len(pool_texts)], common_lens[i])
            if full_prefix:
                rows.append(head)
                continue
            want = max(0, real_lens[i] - common_lens[i] - UNIQUE_TOKENS)
            tail = _fit(tok, suffix_pool[i], want) if want > 0 else ""
            rows.append(head + uniq[i] + tail)
        _write_rows(data_path, rows)

        stats.update(
            prefix_ratio=spec.prefix_ratio,
            prefix_len_max=max(common_lens),
            prefix_pool=len(pool_texts),
            unique_tokens=uniq_n,
            suffix_len_max=max_suffix,
        )
        if not full_prefix:
            hit = sum(c / r for c, r in zip(common_lens, real_lens)) / len(real_lens)
            stats["prefix_hit_ratio_planned"] = round(hit, 4)
            say(f"变长前缀：最大公共前缀 {max(common_lens)}，最长后缀 {max_suffix}，"
                f"计划命中率 {hit:.1%}")

    # ---------- 定长 + 前缀（参考项目的主路径）----------
    else:
        prefix_len = int(spec.input_len * spec.prefix_ratio)
        pool_texts = _build_prefix_pool(tok, corpus, spec, rng, say, length=prefix_len,
                                        should_stop=should_stop)
        prefix_path = _write_prefix_file(out_dir, pool_texts, spec)

        if spec.prefix_ratio >= 1.0:
            # 前缀吃满整个输入长度 —— 参考项目里用来测「同一段 prompt 反复打过来」
            # 的极限命中场景，此时没有后缀也不插唯一 token。
            rows = [pool_texts[i % len(pool_texts)] for i in _each(spec.num, should_stop)]
            _write_rows(data_path, rows)
            stats.update(prefix_ratio=1.0, prefix_len=prefix_len,
                         prefix_pool=len(pool_texts), unique_tokens=0, suffix_len=0)
            say(f"前缀比例 100%：整条 prompt 就是前缀池，共 {len(pool_texts)} 种")
        else:
            suffix_len = max(0, spec.input_len - prefix_len - UNIQUE_TOKENS)
            say(f"生成后缀（{suffix_len} token）+ 唯一 token…")
            suffix = ([_fit(tok, corpus_picker.take(), suffix_len)
                       for _ in _each(spec.num, should_stop)]
                      if suffix_len > 0 else [""] * spec.num)
            uniq = _unique_token_texts(tok, spec.seed, UNIQUE_TOKENS, spec.num)

            say("拼接数据集…")
            rows = [pool_texts[i % len(pool_texts)] + uniq[i] + suffix[i]
                    for i in _each(spec.num, should_stop)]
            _write_rows(data_path, rows)
            stats.update(
                prefix_ratio=spec.prefix_ratio,
                prefix_len=prefix_len,
                prefix_pool=len(pool_texts),
                unique_tokens=UNIQUE_TOKENS,
                suffix_len=suffix_len,
                prefix_hit_ratio_planned=round(prefix_len / spec.input_len, 4),
            )
            say(f"定长前缀：前缀 {prefix_len} + 唯一 {UNIQUE_TOKENS} + 后缀 {suffix_len}")

    if corpus_picker.reset_count:
        say(f"语料被复用了 {corpus_picker.reset_count} 轮（GSM8K 只有 {len(corpus)} 条，"
            "条数多于语料时这是正常的）")

    _measure(tok, data_path, stats, say, should_stop)

    meta_path.write_text(
        json.dumps(
            {"spec": asdict(spec), "rows": spec.num, "stats": stats,
             "tokenizer": str(tokenizer_path)},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    say(f"生成完成：{data_path}")
    return GenResult(dataset_path=data_path, prefix_path=prefix_path,
                     dir=out_dir, cached=False, stats=stats)


def _build_prefix_pool(
    tok: Any, corpus: List[str], spec: Spec, rng: random.Random,
    say: Callable[[str], None], length: int,
    should_stop: Optional[Callable[[], bool]] = None,
) -> List[str]:
    """构造 prefix_num 条**互不相同**、长度都是 length 的前缀。

    互不相同是硬要求：前缀种类数就是这个池子的大小，池子里有重复等于变相
    减少了前缀种类。所以这里的 picker 不允许复用，语料不够时直接报错。
    """
    if spec.prefix_num > len(corpus):
        raise GenError(
            f"前缀个数 {spec.prefix_num} 超过 GSM8K 语料条数 {len(corpus)}，"
            "无法保证每条前缀互不相同。请调小「前缀个数」。"
        )
    say(f"生成前缀池：{spec.prefix_num} 种 × {length} token")
    picker = _Picker(corpus, rng, allow_reuse=False)
    return [_fit(tok, picker.take(), length)
            for _ in _each(spec.prefix_num, should_stop)]


def _write_prefix_file(out_dir: Path, pool_texts: List[str], spec: Spec) -> Path:
    """写预热用的前缀文件。

    每种前缀重复 `dp` 次 —— 对齐参考项目：DP 域之间不共享 KV cache，
    每个域都得各自命中一次，所以预热要覆盖到每个域。
    """
    rows = [t for t in pool_texts for _ in range(spec.dp)]
    path = out_dir / PREFIX_NAME
    _write_rows(path, rows)
    return path


# 回读校验的 token 预算。回读要把产物重新 tokenize 一遍，成本随
# `条数 × 长度` 线性增长 —— 128k × 1000 条就是 1.28 亿 token，白等好几分钟。
# 所以按长度自适应地抽样：短数据集多抽几条，长数据集少抽几条，
# 总的 token 量控制在这个量级。
_MEASURE_TOKEN_BUDGET = 2_000_000
_MEASURE_MIN_ROWS = 20
_MEASURE_MAX_ROWS = 500


def _measure(tok: Any, data_path: Path, stats: Dict[str, Any],
             say: Callable[[str], None],
             should_stop: Optional[Callable[[], bool]] = None) -> None:
    """回读产物，量一下实测长度和前 32 个 token 的前缀复用情况。

    实测长度一定会和设定值有出入（tokenize↔decode 有损），把它写进 stats
    展示给用户，免得用户以为是参数没生效。

    只抽样前若干条：见 `_MEASURE_TOKEN_BUDGET`。抽了多少条记在
    `measured_rows` 里，前端会如实标出这是抽样。
    """
    target_avg = max(1, int(stats.get("target_len_avg") or 1))
    budget_rows = max(_MEASURE_MIN_ROWS,
                      min(_MEASURE_MAX_ROWS, _MEASURE_TOKEN_BUDGET // target_avg))

    lens: List[int] = []
    heads: Dict[tuple, int] = {}
    try:
        with data_path.open(encoding="utf-8") as f:
            for line in f:
                if len(lens) >= budget_rows:
                    break
                if should_stop is not None and should_stop():
                    raise GenCancelled()
                line = line.strip()
                if not line:
                    continue
                text = json.loads(line).get("question", "")
                ids = tok.encode(text, add_special_tokens=False)
                if not ids:
                    continue
                lens.append(len(ids))
                heads[tuple(ids[:32])] = heads.get(tuple(ids[:32]), 0) + 1
    except (OSError, ValueError) as e:  # noqa: BLE001
        log.warning("回读产物失败：%s", e)
        return

    if not lens:
        return
    stats["actual_len_min"] = min(lens)
    stats["actual_len_max"] = max(lens)
    stats["actual_len_avg"] = round(sum(lens) / len(lens), 1)
    stats["measured_rows"] = len(lens)
    stats["distinct_prefix32"] = len(heads)
    dev = (stats["actual_len_avg"] - target_avg) / target_avg
    stats["length_deviation"] = round(dev, 4)
    full = len(lens) >= int(stats.get("rows") or 0)
    scope = "" if full else f"（抽样 {len(lens)}/{stats.get('rows')} 条）"
    say(f"实测长度 {stats['actual_len_min']}~{stats['actual_len_max']}"
        f"（均值 {stats['actual_len_avg']}，与目标相差 {dev:+.1%}）{scope}")
