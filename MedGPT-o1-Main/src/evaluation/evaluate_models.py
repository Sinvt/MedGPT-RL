import argparse
import csv
import gc
import json
import pathlib
import re
import sys
import time
from typing import Any

import torch
from peft import PeftModel
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.rewards.format_reward import format_reward_func
from src.rewards.orm_reward import (
    accuracy_reward_func,
    extract_final_answer,
    score_response as score_orm_response,
)
from src.rewards.process_rule_reward import length_reward_func


DEFAULT_SYSTEM_PROMPT = (
    "你是一个严谨的中文医学推理助手。请基于题干进行医学推理，"
    "先使用 <think>...</think> 写出必要推理过程，再用 `最终答案：` 给出简洁答案。"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Base / SFT / GRPO 统一推理评测脚本")
    parser.add_argument("--base_model", type=str, default="/gemini/pretrain/Qwen2.5-7B-Instruct", help="基础模型路径。")
    parser.add_argument("--sft_lora_path", type=str, default="outputs/sft_qwen2_5_7b_lora_v1", help="SFT LoRA 路径。")
    parser.add_argument("--grpo_lora_path", type=str, default="outputs/grpo_qwen2_5_7b_medical_final", help="GRPO LoRA 路径。")
    parser.add_argument("--questions_file", type=str, default="data/final/rl/rl_val.jsonl", help="评测数据 jsonl。")
    parser.add_argument("--output_dir", type=str, default="reports/eval_compare", help="评测结果输出目录。")
    parser.add_argument(
        "--models",
        type=str,
        default="base,sft,grpo",
        help="要评测的模型，逗号分隔：base,sft,sft_v2_a,grpo。",
    )
    parser.add_argument("--limit", type=int, default=0, help="最多评测多少条；0 表示全部。")
    parser.add_argument("--max_new_tokens", type=int, default=1024, help="最大生成 token 数。")
    parser.add_argument("--temperature", type=float, default=0.7, help="采样温度，仅 --do_sample 生效。")
    parser.add_argument("--top_p", type=float, default=0.9, help="top-p，仅 --do_sample 生效。")
    parser.add_argument("--do_sample", action="store_true", help="开启采样；默认关闭，便于结果可复现。")
    parser.add_argument("--use_dataset_prompt", action=argparse.BooleanOptionalAction, default=True, help="优先使用数据中的 prompt。")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有明细文件；默认会复用已有结果并跳过。")
    parser.add_argument("--merge_lora_for_eval", action="store_true", help="推理前合并 LoRA，通常更省显存但加载更慢。")
    return parser.parse_args()


def load_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line_idx, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            question = extract_question(item)
            if not question:
                raise ValueError(f"{path} 第 {line_idx} 行缺少 question，也无法从 prompt/messages 提取 user 内容。")
            records.append(
                {
                    "id": item.get("id", f"sample_{line_idx}"),
                    "source_id": item.get("source_id"),
                    "source": item.get("source"),
                    "question": question,
                    "prompt": item.get("prompt") or item.get("messages"),
                    "standard_answer": item.get("standard_answer") or item.get("answer") or "",
                    "answer_aliases": item.get("answer_aliases") or [],
                }
            )
    return records


def extract_question(item: dict[str, Any]) -> str:
    if item.get("question"):
        return str(item["question"]).strip()
    messages = item.get("prompt") or item.get("messages") or []
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "user":
                return str(message.get("content", "")).strip()
    return ""


def read_existing_ids(path: pathlib.Path) -> set[str]:
    if not path.exists():
        return set()
    ids = set()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("id"):
                ids.add(str(item["id"]))
    return ids


def build_messages(record: dict[str, Any], args: argparse.Namespace) -> list[dict[str, str]]:
    dataset_prompt = record.get("prompt")
    if args.use_dataset_prompt and isinstance(dataset_prompt, list):
        messages = []
        for message in dataset_prompt:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            if role in {"system", "user", "assistant"} and content:
                messages.append({"role": role, "content": str(content)})
        if messages:
            return messages
    return [
        {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
        {"role": "user", "content": record["question"]},
    ]


def render_prompt(tokenizer: Any, record: dict[str, Any], args: argparse.Namespace) -> str:
    dataset_prompt = record.get("prompt")
    if args.use_dataset_prompt and isinstance(dataset_prompt, str):
        return dataset_prompt
    messages = build_messages(record, args)
    return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)


def check_format(response: str) -> dict[str, Any]:
    think_match = re.search(r"<think>(.*?)</think>", response, flags=re.DOTALL | re.IGNORECASE)
    final_match = re.search(r"最终答案\s*[:：]\s*(.+)", response, flags=re.DOTALL)
    return {
        "has_think": think_match is not None,
        "has_final_prefix": final_match is not None,
        "complete_format": think_match is not None and final_match is not None,
        "think_chars": len(think_match.group(1).strip()) if think_match else 0,
        "final_answer": extract_final_answer(response),
    }


def get_device(model: Any) -> torch.device:
    return next(model.parameters()).device


def load_tokenizer(base_model: str) -> Any:
    tokenizer = AutoTokenizer.from_pretrained(base_model, trust_remote_code=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
        tokenizer.pad_token_id = tokenizer.eos_token_id
    tokenizer.padding_side = "left"
    return tokenizer


def load_stage_model(stage: str, args: argparse.Namespace) -> tuple[Any, Any]:
    print(f"\n========== 加载 {stage.upper()} 模型 ==========")
    tokenizer = load_tokenizer(args.base_model)
    dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        torch_dtype=dtype,
        device_map="auto" if torch.cuda.is_available() else None,
        trust_remote_code=True,
        low_cpu_mem_usage=True,
    )

    if stage in {"sft", "grpo"}:
        print(f"加载 SFT LoRA：{args.sft_lora_path}")
        model = PeftModel.from_pretrained(model, args.sft_lora_path, is_trainable=False)
        if stage == "grpo" or args.merge_lora_for_eval:
            print("合并 SFT LoRA。")
            model = model.merge_and_unload()

    if stage == "grpo":
        print(f"加载 GRPO LoRA：{args.grpo_lora_path}")
        model = PeftModel.from_pretrained(model, args.grpo_lora_path, is_trainable=False)
        if args.merge_lora_for_eval:
            print("合并 GRPO LoRA。")
            model = model.merge_and_unload()

    model.eval()
    model.config.use_cache = True
    print(f"{stage.upper()} 模型就绪，设备：{get_device(model)}")
    return model, tokenizer


def generate_one(model: Any, tokenizer: Any, prompt: str, args: argparse.Namespace) -> str:
    inputs = tokenizer([prompt], return_tensors="pt").to(get_device(model))
    generation_kwargs = {
        "max_new_tokens": args.max_new_tokens,
        "do_sample": args.do_sample,
        "pad_token_id": tokenizer.pad_token_id,
        "eos_token_id": tokenizer.eos_token_id,
        "use_cache": True,
    }
    if args.do_sample:
        generation_kwargs.update({"temperature": args.temperature, "top_p": args.top_p})

    with torch.no_grad():
        outputs = model.generate(**inputs, **generation_kwargs)
    generated_ids = outputs[0][inputs.input_ids.shape[1] :]
    return tokenizer.decode(generated_ids, skip_special_tokens=True).strip()


def score_response(response: str, record: dict[str, Any]) -> dict[str, Any]:
    standard_answer = record.get("standard_answer", "")
    aliases = record.get("answer_aliases") or []
    fmt = check_format(response)
    format_reward = format_reward_func([response])[0]
    accuracy_reward = accuracy_reward_func([response], [standard_answer], [aliases])[0]
    length_reward = length_reward_func([response])[0]
    orm_result = score_orm_response(response, standard_answer, aliases)
    return {
        **fmt,
        "format_reward": format_reward,
        "accuracy_reward": accuracy_reward,
        "length_reward": length_reward,
        "total_reward": format_reward + accuracy_reward + length_reward,
        "orm_score": orm_result.score,
        "orm_hit": orm_result.matched,
        "orm_exact_hit": orm_result.match_type == "exact",
        "orm_match_type": orm_result.match_type,
        "orm_predicted_answer": orm_result.predicted_answer,
        "orm_matched_answer": orm_result.matched_answer,
    }


def evaluate_stage(stage: str, records: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    output_dir = pathlib.Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    detail_path = output_dir / f"{stage}_eval.jsonl"

    existing_ids = set() if args.overwrite else read_existing_ids(detail_path)
    mode = "w" if args.overwrite else "a"
    pending_records = [record for record in records if str(record["id"]) not in existing_ids]
    print(f"\n{stage.upper()} 待评测：{len(pending_records)}/{len(records)} 条；明细：{detail_path}")
    if not pending_records and detail_path.exists():
        print(f"{stage.upper()} 已有完整评测结果，跳过模型加载。")
        summary = summarize_detail(stage, detail_path)
        summary["new_seconds"] = 0.0
        return summary

    model, tokenizer = load_stage_model(stage, args)
    start_time = time.time()
    with detail_path.open(mode, encoding="utf-8") as f:
        for record in tqdm(pending_records, desc=f"评测 {stage}", unit="条"):
            prompt = render_prompt(tokenizer, record, args)
            item_start = time.time()
            response = generate_one(model, tokenizer, prompt, args)
            elapsed = time.time() - item_start
            scores = score_response(response, record)
            out = {
                "id": record["id"],
                "source": record.get("source"),
                "source_id": record.get("source_id"),
                "question": record["question"],
                "standard_answer": record.get("standard_answer"),
                "answer_aliases": record.get("answer_aliases"),
                "model_stage": stage,
                "response": response,
                "seconds": round(elapsed, 4),
                **scores,
            }
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
            f.flush()

    total_time = time.time() - start_time
    del model
    del tokenizer
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    summary = summarize_detail(stage, detail_path)
    summary["new_seconds"] = round(total_time, 2)
    return summary


def summarize_detail(stage: str, path: pathlib.Path) -> dict[str, Any]:
    rows = load_jsonl_loose(path)
    n = len(rows)
    if n == 0:
        return {"model": stage, "samples": 0}

    def avg(key: str) -> float:
        return sum(float(row.get(key, 0) or 0) for row in rows) / n

    return {
        "model": stage,
        "samples": n,
        "think_hit_rate": sum(1 for row in rows if row.get("has_think")) / n,
        "final_prefix_rate": sum(1 for row in rows if row.get("has_final_prefix")) / n,
        "complete_format_rate": sum(1 for row in rows if row.get("complete_format")) / n,
        "orm_hit_rate": sum(1 for row in rows if row.get("orm_hit")) / n,
        "orm_exact_hit_rate": sum(
            1
            for row in rows
            if row.get("orm_exact_hit")
            or (
                "orm_exact_hit" not in row
                and float(row.get("orm_score", 0) or 0) == 1.0
            )
        )
        / n,
        "avg_format_reward": avg("format_reward"),
        "avg_accuracy_reward": avg("accuracy_reward"),
        "avg_length_reward": avg("length_reward"),
        "avg_total_reward": avg("total_reward"),
        "avg_seconds": avg("seconds"),
        "avg_think_chars": avg("think_chars"),
    }


def load_jsonl_loose(path: pathlib.Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def write_summary(summaries: list[dict[str, Any]], output_dir: pathlib.Path) -> None:
    csv_path = output_dir / "model_compare_summary.csv"
    md_path = output_dir / "model_compare_summary.md"
    fields = [
        "model",
        "samples",
        "think_hit_rate",
        "final_prefix_rate",
        "complete_format_rate",
        "orm_hit_rate",
        "orm_exact_hit_rate",
        "avg_format_reward",
        "avg_accuracy_reward",
        "avg_length_reward",
        "avg_total_reward",
        "avg_seconds",
        "avg_think_chars",
        "new_seconds",
    ]

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in summaries:
            writer.writerow({field: row.get(field, "") for field in fields})

    headers = [
        "模型",
        "样本数",
        "ORM精确命中",
        "ORM任意命中",
        "完整格式",
        "平均总奖励",
        "平均耗时/条",
        "平均think字数",
    ]
    lines = [
        "# 统一推理评测汇总",
        "",
        f"- 评测时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 明细目录：`{output_dir.as_posix()}`",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in summaries:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("model", "")),
                    str(row.get("samples", "")),
                    pct(row.get("orm_exact_hit_rate")),
                    pct(row.get("orm_hit_rate")),
                    pct(row.get("complete_format_rate")),
                    f"{float(row.get('avg_total_reward', 0) or 0):.4f}",
                    f"{float(row.get('avg_seconds', 0) or 0):.2f}s",
                    f"{float(row.get('avg_think_chars', 0) or 0):.1f}",
                ]
            )
            + " |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n汇总 CSV 保存至：{csv_path}")
    print(f"汇总 Markdown 保存至：{md_path}")


def pct(value: Any) -> str:
    return f"{float(value or 0) * 100:.2f}%"


def main() -> None:
    args = parse_args()
    records = load_jsonl(pathlib.Path(args.questions_file))
    if args.limit and args.limit > 0:
        records = records[: args.limit]
    if not records:
        raise ValueError("评测数据为空。")

    stages = [item.strip().lower() for item in args.models.split(",") if item.strip()]
    valid_stages = {"base", "sft", "sft_v2_a", "grpo"}
    bad_stages = [stage for stage in stages if stage not in valid_stages]
    if bad_stages:
        raise ValueError(f"未知模型阶段：{bad_stages}，只支持 {sorted(valid_stages)}")

    output_dir = pathlib.Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"统一评测样本数：{len(records)}；模型：{stages}")

    summaries = []
    for stage in stages:
        summaries.append(evaluate_stage(stage, records, args))
        write_summary(summaries, output_dir)

    print("\n========== 统一评测完成 ==========")
    for row in summaries:
        print(
            f"{row['model']}: samples={row['samples']}, "
            f"ORM_exact={pct(row.get('orm_exact_hit_rate'))}, "
            f"ORM_any={pct(row.get('orm_hit_rate'))}, "
            f"format={pct(row.get('complete_format_rate'))}, "
            f"avg_reward={float(row.get('avg_total_reward', 0) or 0):.4f}"
        )


if __name__ == "__main__":
    main()
