"""接口类型与精选数据集目录。

这里的每一条都是对 aisbench 镜像实测核验过的，不要凭直觉改：

- variant 名 = `benchmark/configs/datasets/<family>/<variant>.py` 去掉 `.py`
  （CLI 按文件 basename 匹配，见 utils/file/file.py::match_cfg_file）
- `ready_path` / `extract_into` 都是相对于 datasets cache 根的路径。
  数据根由环境变量 AIS_BENCH_DATASETS_CACHE 指定，网关设为容器的 /work/datasets。
  配置里的 path='ais_bench/datasets/xxx' 会被拼到该根目录下
  （见 datasets/utils/datasets.py::get_data_path）

核验时踩到的坑，已在条目里逐个处理：
- piqa：配置期望 `ais_bench/datasets/physicaliqa-train-dev`，而 OSS 的 piqa.zip 解出的是
  `piqa/`，名字对不上 → 不收录（避免下载完仍然跑不了）。
- ceval：README 让用 modelscope，但那个包解出的是顶层 `dev/val/test/`；
  OSS 的 ceval.zip 才解出 `ceval/formal_ceval/`，与配置一致 → 用 OSS。
- aime2024：aime.zip 解出的是**裸文件** aime.jsonl，配置却期望 `aime/aime.jsonl`
  → extract_into 指向 `aime/` 目录。
"""

from __future__ import annotations

from typing import Any, Dict, List

OSS = "http://opencompass.oss-cn-shanghai.aliyuncs.com/datasets/data"

# 由网关自己合成、而不是从磁盘上读现成文件的数据集（见 dataset_gen.py）。
# 它不在 aisbench 的 family 列表里，配置由 confgen 直接以字面量写出来。
PREFIX_GEN_FAMILY = "prefix_gen"

# ---------------------------------------------------------------- 接口类型
#
# OpenAI 兼容的「协议 × 流式」组合，映射到 benchmark/configs/models/vllm_api/ 下的模板。
# type / stream / returns_tool_calls 三个字段决定实际行为：
#   VLLMCustomAPI     → POST {base}/v1/completions
#   VLLMCustomAPIChat → POST {base}/v1/chat/completions

API_TYPES: List[Dict[str, Any]] = [
    {
        "id": "chat",
        "label": "chat/completions（对话，非流式）",
        "protocol": "chat",
        "stream": False,
        "model_class": "VLLMCustomAPIChat",
        "template": "vllm_api_general_chat",
    },
    {
        "id": "chat_stream",
        "label": "chat/completions（对话，流式）",
        "protocol": "chat",
        "stream": True,
        "model_class": "VLLMCustomAPIChat",
        "template": "vllm_api_stream_chat",
    },
    {
        "id": "completion",
        "label": "completions（补全，非流式）",
        "protocol": "completion",
        "stream": False,
        "model_class": "VLLMCustomAPI",
        "template": "vllm_api_general",
    },
    {
        "id": "completion_stream",
        "label": "completions（补全，流式）",
        "protocol": "completion",
        "stream": True,
        "model_class": "VLLMCustomAPI",
        "template": "vllm_api_general_stream",
    },
    {
        "id": "function_call_chat",
        "label": "chat/completions（Function Call）",
        "protocol": "chat",
        "stream": False,
        "model_class": "VLLMCustomAPIChat",
        "returns_tool_calls": True,
        "template": "vllm_api_function_call_chat",
    },
]

API_TYPES_BY_ID = {a["id"]: a for a in API_TYPES}


# ---------------------------------------------------------------- 数据集

def _ds(
    family: str,
    label: str,
    variants: List[tuple],
    default_variant: str,
    ready_path: str,
    extract_into: str,
    url: str | None,
    size_mb: float = 0.0,
    perf: bool = True,
    note: str = "",
    builtin: bool = False,
    filename: str | None = None,
) -> Dict[str, Any]:
    return {
        "family": family,
        "label": label,
        "variants": [{"id": v, "label": l} for v, l in variants],
        "default_variant": default_variant,
        "ready_path": ready_path,
        "extract_into": extract_into,
        "url": url,
        "filename": filename,
        "size_mb": size_mb,
        "perf": perf,
        "note": note,
        "builtin": builtin,
    }


DATASETS: List[Dict[str, Any]] = [
    _ds(
        "synthetic", "随机数据集（内置）",
        [("synthetic_gen_string", "随机字符串（可配输入/输出长度分布）"),
         ("synthetic_gen_tokenid", "随机 token id（定长）"),
         ("synthetic_gen", "随机（读取 synthetic_config.py）")],
        "synthetic_gen_string",
        ready_path="",  # 内置，不需要磁盘数据
        extract_into="", url=None, builtin=True,
        note="随机生成长度可控的输入输出，性能测试的首选。无需下载。",
    ),
    _ds(
        PREFIX_GEN_FAMILY, "GSM8K 前缀数据集（指定长度 + prefix 比例）",
        [("prefix_gen", "从 GSM8K 取语料，按指定长度与前缀比例合成")],
        "prefix_gen",
        ready_path="",  # 由网关现场合成，不预置磁盘数据
        extract_into="", url=None, builtin=True,
        note="按指定输入长度和前缀复用比例从 GSM8K 合成数据集："
             "做长上下文压测，或测 prefix cache 命中下的性能。需要 tokenizer。",
    ),
    _ds(
        "sharegpt", "ShareGPT 多轮对话",
        [("sharegpt_gen", "多轮对话（吞吐测试）")],
        "sharegpt_gen",
        ready_path="ais_bench/datasets/sharegpt",
        extract_into="ais_bench/datasets/sharegpt",
        url="https://huggingface.co/datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/resolve/main/ShareGPT_V3_unfiltered_cleaned_split.json",
        filename="ShareGPT_V3_unfiltered_cleaned_split.json",
        size_mb=90.0, perf=True,
        note="真实多轮对话分布，适合吞吐压测。需能访问 huggingface.co。仅支持性能模式。",
    ),
    _ds(
        "gsm8k", "GSM8K（小学数学应用题）",
        [("gsm8k_gen_4_shot_cot_chat_prompt", "4-shot CoT（chat）"),
         ("gsm8k_gen_0_shot_cot_chat_prompt", "0-shot CoT（chat）"),
         ("gsm8k_gen_4_shot_cot_str", "4-shot CoT（str）"),
         ("gsm8k_gen_0_shot_cot_str", "0-shot CoT（str）")],
        "gsm8k_gen_4_shot_cot_chat_prompt",
        ready_path="ais_bench/datasets/gsm8k",
        extract_into="ais_bench/datasets",
        url=f"{OSS}/gsm8k.zip", size_mb=3.4,
    ),
    _ds(
        "ceval", "C-Eval（中文综合）",
        [("ceval_gen_5_shot_str", "5-shot（str）"),
         ("ceval_gen_0_shot_str", "0-shot（str）"),
         ("ceval_gen_0_shot_cot_chat_prompt", "0-shot CoT（chat）")],
        "ceval_gen_5_shot_str",
        ready_path="ais_bench/datasets/ceval/formal_ceval",
        extract_into="ais_bench/datasets",
        url=f"{OSS}/ceval.zip", size_mb=4.0,
        note="用 OSS 包而非 README 里写的 modelscope 包，modelscope 包解出的目录结构与配置不匹配。",
    ),
    _ds(
        "mmlu", "MMLU（英文综合）",
        [("mmlu_gen_5_shot_str", "5-shot（str）"),
         ("mmlu_gen_5_shot_chat_prompt", "5-shot（chat）"),
         ("mmlu_gen_0_shot_cot_chat_prompt", "0-shot CoT（chat）")],
        "mmlu_gen_5_shot_str",
        ready_path="ais_bench/datasets/mmlu",
        extract_into="ais_bench/datasets",
        url=f"{OSS}/mmlu.zip", size_mb=2.5,
    ),
    _ds(
        "mmlu_pro", "MMLU-Pro",
        [("mmlu_pro_gen_5_shot_str", "5-shot（str）"),
         ("mmlu_pro_gen_0_shot_str", "0-shot（str）")],
        "mmlu_pro_gen_5_shot_str",
        ready_path="ais_bench/datasets/mmlu_pro",
        extract_into="ais_bench/datasets",
        url=f"{OSS}/mmlu_pro.zip", size_mb=3.6,
    ),
    _ds(
        "cmmlu", "CMMLU（中文综合）",
        [("cmmlu_gen_5_shot_cot_chat_prompt", "5-shot CoT（chat）"),
         ("cmmlu_gen_0_shot_cot_chat_prompt", "0-shot CoT（chat）")],
        "cmmlu_gen_5_shot_cot_chat_prompt",
        ready_path="ais_bench/datasets/cmmlu",
        extract_into="ais_bench/datasets",
        url=f"{OSS}/cmmlu.zip", size_mb=1.1,
    ),
    _ds(
        "gpqa", "GPQA（研究生级问答）",
        [("gpqa_gen_0_shot_str", "0-shot（str）"),
         ("gpqa_gen_0_shot_cot_chat_prompt", "0-shot CoT（chat）")],
        "gpqa_gen_0_shot_str",
        ready_path="ais_bench/datasets/gpqa",
        extract_into="ais_bench/datasets",
        url=f"{OSS}/gpqa.zip", size_mb=2.3,
    ),
    _ds(
        "math", "MATH（数学竞赛）",
        [("math_prm800k_500_0shot_cot_gen", "PRM800K 500 题 0-shot CoT"),
         ("math_prm800k_500_5shot_cot_gen", "PRM800K 500 题 5-shot CoT"),
         ("math500_gen_0_shot_cot_chat_prompt", "MATH-500 0-shot CoT（chat）")],
        "math_prm800k_500_0shot_cot_gen",
        ready_path="ais_bench/datasets/math",
        extract_into="ais_bench/datasets",
        url=f"{OSS}/math.zip", size_mb=4.4,
    ),
    _ds(
        "aime2024", "AIME 2024（数学竞赛）",
        [("aime2024_gen_0_shot_str", "0-shot（str）"),
         ("aime2024_gen_0_shot_chat_prompt", "0-shot（chat）")],
        "aime2024_gen_0_shot_str",
        ready_path="ais_bench/datasets/aime/aime.jsonl",
        extract_into="ais_bench/datasets/aime",
        url=f"{OSS}/aime.zip", size_mb=0.05,
        note="aime.zip 解出的是裸文件 aime.jsonl，需放进 aime/ 目录才匹配配置。",
    ),
    _ds(
        "humaneval", "HumanEval（代码生成）",
        [("humaneval_gen_0_shot", "0-shot（str）")],
        "humaneval_gen_0_shot",
        ready_path="ais_bench/datasets/humaneval/human-eval-v2-20210705.jsonl",
        extract_into="ais_bench/datasets",
        url=f"{OSS}/humaneval.zip", size_mb=0.05,
    ),
    _ds(
        "mbpp", "MBPP（Python 编程）",
        [("mbpp_passk_gen_3_shot_chat_prompt", "3-shot pass@k（chat）"),
         ("sanitized_mbpp_passk_gen_3_shot_chat_prompt", "sanitized 3-shot pass@k（chat）")],
        "mbpp_passk_gen_3_shot_chat_prompt",
        ready_path="ais_bench/datasets/mbpp/mbpp.jsonl",
        extract_into="ais_bench/datasets",
        url=f"{OSS}/mbpp.zip", size_mb=0.2,
    ),
    _ds(
        "ifeval", "IFEval（指令遵循）",
        [("ifeval_0_shot_gen_str", "0-shot（str）")],
        "ifeval_0_shot_gen_str",
        ready_path="ais_bench/datasets/ifeval/input_data.jsonl",
        extract_into="ais_bench/datasets",
        url=f"{OSS}/ifeval.zip", size_mb=0.05,
    ),
    _ds(
        "hellaswag", "HellaSwag（常识推理）",
        [("hellaswag_gen_10_shot_chat_prompt", "10-shot（chat）"),
         ("hellaswag_gen_0_shot_chat_prompt", "0-shot（chat）")],
        "hellaswag_gen_10_shot_chat_prompt",
        ready_path="ais_bench/datasets/hellaswag/hellaswag.jsonl",
        extract_into="ais_bench/datasets",
        url=f"{OSS}/hellaswag.zip", size_mb=2.8,
    ),
    _ds(
        "triviaqa", "TriviaQA（阅读理解）",
        [("triviaqa_gen_5_shot_chat_prompt", "5-shot（chat）")],
        "triviaqa_gen_5_shot_chat_prompt",
        ready_path="ais_bench/datasets/triviaqa",
        extract_into="ais_bench/datasets",
        url=f"{OSS}/triviaqa.zip", size_mb=6.1,
    ),
    _ds(
        "winogrande", "WinoGrande（指代消解）",
        [("winogrande_gen_5_shot_chat_prompt", "5-shot（chat）"),
         ("winogrande_gen_0_shot_chat_prompt", "0-shot（chat）")],
        "winogrande_gen_5_shot_chat_prompt",
        ready_path="ais_bench/datasets/winogrande",
        extract_into="ais_bench/datasets",
        url=f"{OSS}/winogrande.zip", size_mb=3.4,
    ),
    _ds(
        "drop", "DROP（离散推理）",
        [("drop_gen_3_shot_str", "3-shot（str）"),
         ("drop_gen_0_shot_str", "0-shot（str）")],
        "drop_gen_3_shot_str",
        ready_path="ais_bench/datasets/drop_simple_eval",
        extract_into="ais_bench/datasets",
        url=f"{OSS}/drop_simple_eval.zip", size_mb=3.7,
    ),
]

DATASETS_BY_FAMILY = {d["family"]: d for d in DATASETS}

# 只能跑性能模式的数据集。两类原因不同，报错时要说清楚是哪一类：
#   PERF_ONLY_FAMILIES —— aisbench 自己的限制（datasets/utils/datasets.py::ONLY_PERF_DATASETS）
#   PREFIX_GEN_FAMILY  —— 网关的限制：合成出来的 answer 是占位符，精度得分没有意义
PERF_ONLY_FAMILIES = {"synthetic", "sharegpt"}


def is_perf_only(family: str) -> bool:
    return family in PERF_ONLY_FAMILIES or family == PREFIX_GEN_FAMILY


def get_dataset(family: str) -> Dict[str, Any] | None:
    return DATASETS_BY_FAMILY.get(family)


def resolve_variant(family: str) -> tuple[str, str]:
    """返回 (family, 默认 variant)。"""
    d = get_dataset(family)
    if not d:
        raise KeyError(f"未知数据集: {family}")
    return family, d["default_variant"]
