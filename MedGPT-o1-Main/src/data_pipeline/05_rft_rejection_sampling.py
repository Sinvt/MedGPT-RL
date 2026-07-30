"""05_rft_rejection_sampling.py — RFT 拒绝采样脚本（Phase 5）

使用本地 vLLM 离线引擎对 rl_train.jsonl 中的训练题进行多路候选生成，
并通过 ORM 评分做初步筛选。支持断点续跑，输出三份 JSONL 文件。

用法:
    # 烟雾测试（20 题 × 4 候选）
    python src/data_pipeline/05_rft_rejection_sampling.py \\
        --limit 20 --n 4 --model /path/to/sft_merged_v1

    # 全量生成（7016 题 × 6 候选）
    python src/data_pipeline/05_rft_rejection_sampling.py \\
        --n 6 --model /path/to/sft_merged_v1
"""

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from rewards.orm_reward import score_response  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 默认路径
# ---------------------------------------------------------------------------

DEFAULT_INPUT = PROJECT_ROOT / "data" / "final" / "rl" / "rl_train.jsonl"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "intermediate" / "rft"

SYSTEM_PROMPT = (
    "你是一个严谨的中文医学推理助手。请基于题干进行医学推理，"
    "先使用 <think>...</think> 写出必要推理过程，再用 `最终答案：` 给出简洁答案。"
)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def get_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes}m{secs}s"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"找不到输入文件：{path}")
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def append_jsonl(path: Path, records: list[dict]) -> None:
    """追加写入 JSONL，用于增量持久化。"""
    with open(path, "a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_done_keys(path: Path) -> set[tuple[str, int]]:
    """从已有输出文件加载已完成的 (question_id, candidate_index) 集合。"""
    done = set()
    if not path.exists():
        return done
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                # 优先读 question_id（新格式），回退到 id（兼容旧烟雾测试文件）
                qid = get_text(obj.get("question_id")) or get_text(obj.get("id"))
                cidx = int(obj.get("candidate_index", -1))
                if qid and cidx >= 0:
                    done.add((qid, cidx))
            except (json.JSONDecodeError, ValueError):
                continue
    return done


def has_think_structure(text: str) -> bool:
    """检查生成内容是否包含 <think>...</think> 结构。"""
    return bool(re.search(r"<think>.*?</think>", text, flags=re.DOTALL | re.IGNORECASE))


def has_final_answer_marker(text: str) -> bool:
    """检查生成内容是否包含最终答案标记。"""
    return bool(re.search(r"(最终答案|答案|结论|诊断)\s*[:：]", text))


def count_think_steps(text: str) -> int:
    """统计 <think> 区域内的推理步骤数。"""
    match = re.search(r"<think>(.*?)</think>", text, flags=re.DOTALL | re.IGNORECASE)
    if not match:
        return 0
    think_text = match.group(1).strip()
    lines = [line.strip() for line in think_text.splitlines() if line.strip()]
    steps = []
    for line in lines:
        line = re.sub(r"^\s*(?:\d+[\.)\、)]|[-*])\s*", "", line).strip()
        if line:
            steps.append(line)
    if len(steps) <= 1 and think_text:
        parts = re.split(r"(?<=[。！？!?])\s*", think_text)
        steps = [p.strip() for p in parts if p.strip()]
    return len(steps)


LEAKAGE_PATTERNS = [
    r"标准答案",
    r"给定答案",
    r"已知答案",
    r"根据答案",
    r"答案已[经]?给出",
    r"题目要求.*答案",
    r"作为.*标注",
]

OPTION_LEAK_PATTERNS = [
    r"(?:^|[\n\r])\s*[A-H][\.．、:：]\s*",
    r"选项[A-H]",
    r"正确选项",
    r"错误选项",
    r"下列哪[一]?项",
    r"以下哪[一]?项",
]

PLACEHOLDER_PATTERNS = [
    r"无法判断",
    r"信息不足",
    r"不确定",
    r"需要更多信息",
    r"不能确定",
]


def normalize_text(text: str) -> str:
    text = get_text(text).lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，,。.;；:：、!?！？（）()\[\]【】\"'“”‘’]", "", text)
    return text


def extract_steps(think_text: str) -> list[str]:
    lines = [line.strip() for line in think_text.splitlines() if line.strip()]
    steps = []
    for line in lines:
        line = re.sub(r"^\s*(?:\d+[\.\)、)]|[-*])\s*", "", line).strip()
        if line:
            steps.append(line)

    if len(steps) <= 1 and think_text:
        parts = re.split(r"(?<=[。！？!?])\s*", think_text)
        steps = [part.strip() for part in parts if part.strip()]
    return steps


def contains_any_pattern(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return pattern
    return None


def duplicate_step_count(steps: list[str]) -> int:
    from collections import Counter
    normalized_steps = [normalize_text(step) for step in steps if normalize_text(step)]
    counts = Counter(normalized_steps)
    return sum(count - 1 for count in counts.values() if count > 1)


# ---------------------------------------------------------------------------
# 核心：分块生成 + 评估 + 持久化
# ---------------------------------------------------------------------------


def build_prompt(item: dict) -> str:
    """从 rl_train.jsonl 的一条记录构造单轮对话 prompt。

    注意：vLLM offline 生成通常直接传原始文本；
    如果模型需要 chat template，vLLM 的 LLM 类支持
    通过 tokenizer.apply_chat_template 自动处理。
    这里我们构造 messages 列表，交给 vLLM 的 chat 接口。
    """
    question = get_text(item.get("question"))
    return question


def build_chat_messages(item: dict) -> list[dict]:
    """构造 ChatML messages 列表，优先复用数据自带的 prompt。"""
    if "prompt" in item and isinstance(item["prompt"], list):
        return item["prompt"]
    
    question = get_text(item.get("question"))
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]


def evaluate_candidate(
    generated_text: str,
    item: dict,
    candidate_index: int,
    model_name: str,
    sampling_params_dict: dict,
) -> dict:
    """对单条生成结果进行 ORM 评分和结构检查，返回完整记录。"""
    question_id = get_text(item.get("id")) or get_text(item.get("source_id")) or "unknown"
    standard_answer = get_text(item.get("standard_answer"))
    answer_aliases = item.get("answer_aliases", [])
    if not isinstance(answer_aliases, list):
        answer_aliases = []

    # ORM 结构化评分
    orm_result = score_response(generated_text, standard_answer, answer_aliases)

    # 结构检查
    think_ok = has_think_structure(generated_text)
    answer_marker_ok = has_final_answer_marker(generated_text)
    step_count = count_think_steps(generated_text)
    think_match = re.search(r"<think>(.*?)</think>", generated_text, flags=re.DOTALL | re.IGNORECASE)
    think_chars = len(think_match.group(1).strip()) if think_match else 0

    # 构造拒绝原因列表
    reject_reasons = []
    if not think_ok:
        reject_reasons.append("缺少 <think> 结构")
    if not answer_marker_ok:
        reject_reasons.append("缺少最终答案标记")
    if orm_result.match_type == "none":
        reject_reasons.append("ORM 未命中标准答案")
    elif orm_result.match_type in ("contain_pred", "contain_resp"):
        reject_reasons.append(f"ORM 仅包含匹配 ({orm_result.match_type}, score={orm_result.score})")
    if step_count < 2:
        reject_reasons.append(f"推理步骤过少: {step_count}")
    if think_chars > 2000:
        reject_reasons.append(f"推理内容过长: {think_chars} 字")

    # 高阶数据纯净度检查 (复用旧规则)
    leakage = contains_any_pattern(generated_text, LEAKAGE_PATTERNS)
    if leakage:
        reject_reasons.append(f"答案泄漏 ({leakage})")
    
    option_leak = contains_any_pattern(generated_text, OPTION_LEAK_PATTERNS)
    if option_leak:
        reject_reasons.append(f"选项痕迹 ({option_leak})")

    placeholder = contains_any_pattern(generated_text, PLACEHOLDER_PATTERNS)
    if placeholder:
        reject_reasons.append(f"占位回答 ({placeholder})")

    steps_list = extract_steps(think_match.group(1).strip() if think_match else "")
    dup_steps = duplicate_step_count(steps_list)
    if dup_steps > 0:
        reject_reasons.append(f"重复步骤 ({dup_steps} 个)")

    # 严格通过条件：精确匹配 + 结构完整 + 无脏数据
    strict_pass = (
        orm_result.match_type == "exact"
        and think_ok
        and answer_marker_ok
        and step_count >= 2
        and not leakage
        and not option_leak
        and not placeholder
        and dup_steps == 0
    )

    # 全局唯一 ID：rft_{question_id}_{candidate_index}
    unique_id = f"rft_{question_id}_{candidate_index}"

    return {
        "id": unique_id,
        "question_id": question_id,
        "candidate_index": candidate_index,
        "source_id": get_text(item.get("source_id")),
        "openqa_id": get_text(item.get("openqa_id")),
        "source": get_text(item.get("source")),
        "split": get_text(item.get("split")) or get_text(item.get("original_split")) or "train",
        "question": get_text(item.get("question")),
        "standard_answer": standard_answer,
        "answer_aliases": answer_aliases,
        "cot_content": generated_text,
        "final_answer": orm_result.predicted_answer,
        "orm_score": orm_result.score,
        "orm_matched": orm_result.matched,
        "orm_match_type": orm_result.match_type,
        "orm_matched_answer": orm_result.matched_answer,
        "has_think": think_ok,
        "has_answer_marker": answer_marker_ok,
        "quality_step_count": step_count,
        "quality_think_chars": think_chars,
        "quality_score": 100.0 if strict_pass else 0.0,
        "strict_pass": strict_pass,
        "reject_reasons": reject_reasons,
        "model": model_name,
        "sampling_params": sampling_params_dict,
    }


def process_chunk(
    llm,
    sampling_params,
    chunk_items: list[dict],
    n_candidates: int,
    done_keys: set[tuple[str, int]],
    model_name: str,
    sampling_params_dict: dict,
    all_file: Path,
    pass_file: Path,
    reject_file: Path,
) -> dict:
    """处理一个 chunk 的题目：生成 → 评估 → 分流写入三份文件。

    返回本 chunk 的统计信息。
    """
    # 筛选出本 chunk 中尚未完成的 (item, candidate_index) 对
    pending = []
    pending_meta = []  # 与 pending 一一对应的 (item, candidate_index)
    for item in chunk_items:
        qid = get_text(item.get("id")) or get_text(item.get("source_id")) or "unknown"
        for cidx in range(n_candidates):
            if (qid, cidx) in done_keys:
                continue
            pending.append(item)
            pending_meta.append((item, cidx))

    if not pending:
        return {"generated": 0, "strict_pass": 0, "rejected": 0, "skipped": len(chunk_items) * n_candidates}

    # 构造 prompts：对每条待生成记录单独构造一条 prompt
    # 使用 vLLM 的 chat 接口：传 messages 列表
    prompts = [build_chat_messages(item) for item in pending]

    # 调用 vLLM 生成（每条 prompt 生成 1 条，因为我们已经展开了 n_candidates）
    from vllm import SamplingParams as _SP  # 延迟导入避免非 GPU 环境报错
    outputs = llm.chat(prompts, sampling_params)

    # 安全校验：确保 vLLM 返回数量与请求数量一致
    if len(outputs) != len(pending_meta):
        raise RuntimeError(
            f"vLLM output count mismatch: {len(outputs)} != {len(pending_meta)}"
        )

    # 评估并分流
    all_records = []
    pass_records = []
    reject_records = []

    for output, (item, cidx) in zip(outputs, pending_meta):
        generated_text = output.outputs[0].text

        record = evaluate_candidate(
            generated_text=generated_text,
            item=item,
            candidate_index=cidx,
            model_name=model_name,
            sampling_params_dict=sampling_params_dict,
        )

        all_records.append(record)
        if record["strict_pass"]:
            pass_records.append(record)
        else:
            reject_records.append(record)

    # 立即持久化到磁盘
    if all_records:
        append_jsonl(all_file, all_records)
    if pass_records:
        append_jsonl(pass_file, pass_records)
    if reject_records:
        append_jsonl(reject_file, reject_records)

    return {
        "generated": len(all_records),
        "strict_pass": len(pass_records),
        "rejected": len(reject_records),
        "skipped": len(chunk_items) * n_candidates - len(pending),
        "processed_keys": [(get_text(r["id"]), r["candidate_index"]) for r in all_records],
    }


# ---------------------------------------------------------------------------
# 报告生成
# ---------------------------------------------------------------------------


def build_report(
    args,
    total_questions: int,
    total_generated: int,
    total_pass: int,
    total_rejected: int,
    total_skipped: int,
    elapsed: float,
) -> str:
    pass_rate = total_pass / total_generated * 100 if total_generated else 0.0
    return f"""# RFT 拒绝采样报告

## 基本配置

- 模型：`{args.model}`
- 输入文件：`{args.input}`
- 输入题目数：{total_questions}
- 每题候选数：{args.n}
- 批次大小：{args.chunk_size} 题/chunk
- 温度：{args.temperature}
- 最大生成 token：{args.max_tokens}

## 生成结果

- 总生成候选数：{total_generated}
- 严格通过（exact match + 结构完整）：{total_pass} ({pass_rate:.2f}%)
- 拒绝样本数：{total_rejected}
- 断点跳过数：{total_skipped}
- 总耗时：{format_duration(elapsed)}

## 输出文件

- 全量候选：`{args.output_dir / 'rft_all.jsonl'}`
- 严格通过：`{args.output_dir / 'rft_strict_pass.jsonl'}`
- 拒绝样本：`{args.output_dir / 'rft_rejected.jsonl'}`

## 筛选标准

- ORM match_type == "exact" (score 1.0)
- 包含 `<think>...</think>` 结构
- 包含最终答案标记
- 推理步骤 >= 2
- 包含匹配 (score 0.9/0.7) 记录在拒绝样本中供人工审查，不混入 SFT-v2 主训练集
"""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="RFT 拒绝采样：本地 vLLM 多路生成 + ORM 筛选")
    parser.add_argument("--model", type=str, required=True, help="本地模型路径，如 /gemini/code/MedGPT-o1-Main/outputs/sft_merged_v1")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="rl_train.jsonl 路径")
    parser.add_argument("--output_dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="输出目录")
    parser.add_argument("--n", type=int, default=6, help="每题生成候选数")
    parser.add_argument("--chunk_size", type=int, default=128, help="每批处理多少题（非候选数）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少题；0 表示全量")
    parser.add_argument("--temperature", type=float, default=0.7, help="生成温度")
    parser.add_argument("--max_tokens", type=int, default=1024, help="每条候选最大生成 token 数")
    parser.add_argument("--top_p", type=float, default=0.95, help="Top-p 采样")
    parser.add_argument("--gpu_memory_utilization", type=float, default=0.80, help="vLLM GPU 显存利用率")
    parser.add_argument("--max_model_len", type=int, default=2048, help="vLLM 最大上下文长度")
    parser.add_argument("--disable_custom_all_reduce", action="store_true", default=True, help="vLLM 禁用 custom all-reduce")
    parser.add_argument("--report", type=Path, default=None, help="报告输出路径；默认为 output_dir/rft_report.md")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.report is None:
        args.report = args.output_dir / "rft_report.md"

    # 输出文件路径
    all_file = args.output_dir / "rft_all.jsonl"
    pass_file = args.output_dir / "rft_strict_pass.jsonl"
    reject_file = args.output_dir / "rft_rejected.jsonl"

    # 加载断点
    done_keys_all = load_done_keys(all_file)
    logger.info(f"已加载断点：{len(done_keys_all)} 条已完成的 (question_id, candidate_index) 对")

    # 加载输入数据
    items = load_jsonl(args.input)
    if args.limit > 0:
        items = items[:args.limit]
    total_questions = len(items)
    logger.info(f"输入题目数：{total_questions}，每题 {args.n} 候选，共 {total_questions * args.n} 条待生成")

    # 初始化 vLLM
    logger.info(f"正在初始化 vLLM 引擎：{args.model}")
    from vllm import LLM, SamplingParams

    llm = LLM(
        model=args.model,
        dtype="bfloat16",
        tokenizer_mode="slow",
        gpu_memory_utilization=args.gpu_memory_utilization,
        max_model_len=args.max_model_len,
        enforce_eager=True,
        disable_custom_all_reduce=args.disable_custom_all_reduce,
        trust_remote_code=True,
    )

    sampling_params = SamplingParams(
        temperature=args.temperature,
        top_p=args.top_p,
        max_tokens=args.max_tokens,
    )

    sampling_params_dict = {
        "temperature": args.temperature,
        "top_p": args.top_p,
        "max_tokens": args.max_tokens,
    }

    model_name = args.model

    # 分块生成
    total_generated = 0
    total_pass = 0
    total_rejected = 0
    total_skipped = 0
    run_start = time.perf_counter()

    num_chunks = (total_questions + args.chunk_size - 1) // args.chunk_size
    for chunk_idx in range(num_chunks):
        start = chunk_idx * args.chunk_size
        end = min(start + args.chunk_size, total_questions)
        chunk_items = items[start:end]

        chunk_start = time.perf_counter()
        stats = process_chunk(
            llm=llm,
            sampling_params=sampling_params,
            chunk_items=chunk_items,
            n_candidates=args.n,
            done_keys=done_keys_all,
            model_name=model_name,
            sampling_params_dict=sampling_params_dict,
            all_file=all_file,
            pass_file=pass_file,
            reject_file=reject_file,
        )
        chunk_elapsed = time.perf_counter() - chunk_start

        total_generated += stats["generated"]
        total_pass += stats["strict_pass"]
        total_rejected += stats["rejected"]
        total_skipped += stats["skipped"]

        # 更新断点集合（本 chunk 实际产出的新记录）
        for qid, cidx in stats.get("processed_keys", []):
            done_keys_all.add((qid, cidx))

        elapsed_total = time.perf_counter() - run_start
        speed = total_generated / elapsed_total * 60 if elapsed_total > 0 else 0
        pass_rate = total_pass / total_generated * 100 if total_generated else 0

        logger.info(
            f"Chunk {chunk_idx + 1}/{num_chunks} 完成 | "
            f"本批 {stats['generated']} 条 ({chunk_elapsed:.1f}s) | "
            f"累计生成 {total_generated} | 通过 {total_pass} ({pass_rate:.1f}%) | "
            f"拒绝 {total_rejected} | 跳过 {total_skipped} | "
            f"速度 {speed:.1f} 条/min | 总耗时 {format_duration(elapsed_total)}"
        )

    elapsed = time.perf_counter() - run_start

    # 写报告
    report = build_report(args, total_questions, total_generated, total_pass, total_rejected, total_skipped, elapsed)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")

    logger.info("=" * 60)
    logger.info(f"RFT 拒绝采样完成！")
    logger.info(f"总耗时：{format_duration(elapsed)}")
    logger.info(f"生成：{total_generated} | 通过：{total_pass} | 拒绝：{total_rejected} | 跳过：{total_skipped}")
    logger.info(f"全量候选：{all_file}")
    logger.info(f"严格通过：{pass_file}")
    logger.info(f"拒绝样本：{reject_file}")
    logger.info(f"报告：{args.report}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
