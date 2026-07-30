import argparse
import json
import pathlib
import re
import sys
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

from src.rewards.archive.format_reward import format_reward_func
from src.rewards.archive.orm_reward import accuracy_reward_func, medical_orm_score
from src.rewards.archive.process_rule_reward import length_reward_func
from src.rewards.hard_constraints import extract_final_answer


DEFAULT_SYSTEM_PROMPT = (
    "你是一个严谨的中文医学推理助手。请基于题干进行医学推理，"
    "先使用 <think>...</think> 写出必要推理过程，再用 `最终答案：` 给出简洁答案。"
)

DEFAULT_QUESTIONS = [
    {
        "id": "demo_1",
        "question": "患者，男，65岁。突发胸痛2小时，伴大汗淋漓，放射至左肩。心电图示V1-V5导联ST段弓背向上抬高。最可能的诊断是什么？",
        "standard_answer": "急性前壁心肌梗死",
        "answer_aliases": ["前壁心肌梗死", "急性心肌梗死"],
    },
    {
        "id": "demo_2",
        "question": "二甲双胍的主要降糖机制是什么？",
        "standard_answer": "减少肝糖输出",
        "answer_aliases": ["抑制肝糖异生", "降低肝糖生成"],
    },
    {
        "id": "demo_3",
        "question": "儿童支原体肺炎的首选治疗药物是哪一类抗生素？",
        "standard_answer": "大环内酯类",
        "answer_aliases": ["大环内酯类抗生素", "阿奇霉素"],
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MedGPT-o1 GRPO 推理与奖励检查脚本")
    parser.add_argument("--base_model", type=str, default="/gemini/pretrain/Qwen2.5-7B-Instruct", help="基础模型路径。")
    parser.add_argument("--sft_lora_path", type=str, default="outputs/sft_qwen2_5_7b_lora_v1", help="SFT LoRA adapter 路径。")
    parser.add_argument("--grpo_lora_path", type=str, default="outputs/grpo_qwen2_5_7b_medical_final", help="GRPO LoRA adapter 路径。")
    parser.add_argument("--questions_file", type=str, default="data/final/rl/rl_val.jsonl", help="待推理问题 jsonl 文件；传空字符串则使用内置 demo。")
    parser.add_argument("--output_file", type=str, default="reports/grpo_infer_smoke.jsonl", help="推理结果 jsonl 输出路径。")
    parser.add_argument("--system_prompt", type=str, default=DEFAULT_SYSTEM_PROMPT, help="不使用数据集 prompt 时的 system prompt。")
    parser.add_argument("--limit", type=int, default=20, help="最多推理多少条；0 表示全部。")
    parser.add_argument("--use_dataset_prompt", action=argparse.BooleanOptionalAction, default=True, help="优先使用数据集中已经构造好的 prompt。")
    parser.add_argument("--max_new_tokens", type=int, default=1024, help="最大生成 token 数。")
    parser.add_argument("--temperature", type=float, default=0.7, help="采样温度，仅在 --do_sample 时生效。")
    parser.add_argument("--top_p", type=float, default=0.9, help="top-p 采样参数，仅在 --do_sample 时生效。")
    parser.add_argument("--do_sample", action="store_true", help="开启采样；默认关闭，便于稳定评估。")
    parser.add_argument("--merge_grpo_lora", action="store_true", help="将 GRPO LoRA 合并进模型后推理。")
    return parser.parse_args()


def load_jsonl(path: str) -> list[dict[str, Any]]:
    records = []
    with pathlib.Path(path).open("r", encoding="utf-8") as f:
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
                    "standard_answer": item.get("standard_answer") or item.get("answer"),
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


def load_questions(path: str | None, limit: int) -> list[dict[str, Any]]:
    if not path:
        records = DEFAULT_QUESTIONS
    else:
        records = load_jsonl(path)
    if limit and limit > 0:
        records = records[:limit]
    return records


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
        {"role": "system", "content": args.system_prompt},
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
    final_answer = extract_final_answer(response)
    return {
        "has_think_open": "<think>" in response,
        "has_think_close": "</think>" in response,
        "has_think_block": think_match is not None,
        "has_final_prefix": final_match is not None,
        "think_chars": len(think_match.group(1).strip()) if think_match else 0,
        "final_answer": final_answer,
    }


def get_model_device(model: Any) -> torch.device:
    return next(model.parameters()).device


def load_model(args: argparse.Namespace) -> tuple[Any, Any]:
    print(f"加载 tokenizer：{args.base_model}")
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
        tokenizer.pad_token_id = tokenizer.eos_token_id
    tokenizer.padding_side = "left"

    dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
    print(f"加载基础模型：{args.base_model}；dtype={dtype}")
    base_model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        torch_dtype=dtype,
        device_map="auto" if torch.cuda.is_available() else None,
        trust_remote_code=True,
        low_cpu_mem_usage=True,
    )

    print(f"加载并合并 SFT LoRA：{args.sft_lora_path}")
    sft_model = PeftModel.from_pretrained(base_model, args.sft_lora_path, is_trainable=False)
    model = sft_model.merge_and_unload()

    print(f"加载 GRPO LoRA：{args.grpo_lora_path}")
    model = PeftModel.from_pretrained(model, args.grpo_lora_path, is_trainable=False)
    if args.merge_grpo_lora:
        print("合并 GRPO LoRA 后推理。")
        model = model.merge_and_unload()

    model.eval()
    model.config.use_cache = True
    print(f"模型加载完成，当前设备：{get_model_device(model)}")
    return model, tokenizer


def generate_one(model: Any, tokenizer: Any, prompt: str, args: argparse.Namespace) -> str:
    inputs = tokenizer([prompt], return_tensors="pt").to(get_model_device(model))
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
    standard_answer = record.get("standard_answer")
    answer_aliases = record.get("answer_aliases") or []
    format_reward = format_reward_func([response])[0]
    length_reward = length_reward_func([response])[0]

    if standard_answer:
        orm_score = medical_orm_score(response, standard_answer, answer_aliases)
        accuracy_reward = accuracy_reward_func([response], [standard_answer], [answer_aliases])[0]
        total_reward = format_reward + accuracy_reward + length_reward
    else:
        orm_score = None
        accuracy_reward = None
        total_reward = format_reward + length_reward

    return {
        "orm_score": orm_score,
        "format_reward": format_reward,
        "accuracy_reward": accuracy_reward,
        "length_reward": length_reward,
        "total_reward": total_reward,
    }


def print_summary(results: list[dict[str, Any]], output_file: str | None) -> None:
    total = len(results)
    if total == 0:
        print("没有可统计的推理结果。")
        return

    think_count = sum(item["has_think_block"] for item in results)
    final_count = sum(item["has_final_prefix"] for item in results)
    full_format_count = sum(item["has_think_block"] and item["has_final_prefix"] for item in results)
    scored = [item for item in results if item.get("orm_score") is not None]
    orm_hit = sum(1 for item in scored if item["orm_score"] > 0)

    def avg(key: str, items: list[dict[str, Any]]) -> float:
        values = [item[key] for item in items if item.get(key) is not None]
        return sum(values) / len(values) if values else 0.0

    print("\n" + "=" * 80)
    print("GRPO 推理统计")
    print("=" * 80)
    print(f"样本数：{total}")
    print(f"<think>...</think> 命中：{think_count}/{total} ({think_count / total:.2%})")
    print(f"最终答案前缀命中：{final_count}/{total} ({final_count / total:.2%})")
    print(f"完整格式命中：{full_format_count}/{total} ({full_format_count / total:.2%})")
    if scored:
        print(f"ORM 命中：{orm_hit}/{len(scored)} ({orm_hit / len(scored):.2%})")
    print(f"平均 format_reward：{avg('format_reward', results):.4f}")
    print(f"平均 accuracy_reward：{avg('accuracy_reward', results):.4f}")
    print(f"平均 length_reward：{avg('length_reward', results):.4f}")
    print(f"平均 total_reward：{avg('total_reward', results):.4f}")
    if output_file:
        print(f"推理结果已保存至：{output_file}")


def main() -> None:
    args = parse_args()
    model, tokenizer = load_model(args)
    records = load_questions(args.questions_file, args.limit)
    print(f"开始 GRPO 推理，共 {len(records)} 道题。采样模式：{args.do_sample}")

    output_handle = None
    if args.output_file:
        output_path = pathlib.Path(args.output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_handle = output_path.open("w", encoding="utf-8")

    results = []
    try:
        for idx, record in enumerate(tqdm(records, desc="推理进度"), start=1):
            prompt = render_prompt(tokenizer, record, args)
            response = generate_one(model, tokenizer, prompt, args)
            format_info = check_format(response)
            reward_info = score_response(response, record)
            result = {
                **record,
                "response": response,
                **format_info,
                **reward_info,
            }
            result.pop("prompt", None)
            results.append(result)
            if output_handle:
                output_handle.write(json.dumps(result, ensure_ascii=False) + "\n")

            print("\n" + "=" * 80)
            print(f"测试问题 {idx}: {record['question']}")
            if record.get("standard_answer"):
                print(f"标准答案：{record['standard_answer']}")
            print("-" * 80)
            print(response)
            print("-" * 80)
            print(
                "奖励："
                f"format={reward_info['format_reward']:.2f}, "
                f"accuracy={reward_info['accuracy_reward'] if reward_info['accuracy_reward'] is not None else 'NA'}, "
                f"length={reward_info['length_reward']:.2f}, "
                f"total={reward_info['total_reward']:.2f}"
            )
    finally:
        if output_handle:
            output_handle.close()

    print_summary(results, args.output_file)


if __name__ == "__main__":
    main()
