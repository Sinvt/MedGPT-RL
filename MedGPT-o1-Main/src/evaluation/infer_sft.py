import argparse
import json
import re
from pathlib import Path
from typing import Any

import torch
from peft import PeftModel
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer


DEFAULT_SYSTEM_PROMPT = "你是一个严谨的中文医学推理助手。请基于题干进行医学推理，并给出最终答案。"

DEFAULT_QUESTIONS = [
    "患者，男，65岁。突发胸痛2小时，伴大汗淋漓，放射至左肩。心电图示V1-V5导联ST段弓背向上抬高。最可能的诊断是什么？",
    "二甲双胍的主要降糖机制是什么？",
    "儿童支原体肺炎的首选治疗药物是哪一类抗生素？",
    "高血压伴有痛风的患者，应慎用或禁用哪类降压药？",
    "急性阑尾炎的典型临床表现是什么？",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MedGPT-o1 SFT 推理与格式检查脚本")
    parser.add_argument("--base_model", type=str, default="/gemini/pretrain/Qwen2.5-7B-Instruct", help="基础模型路径。")
    parser.add_argument("--lora_path", type=str, default="outputs/sft_qwen2_5_7b_lora", help="LoRA adapter 路径。")
    parser.add_argument("--questions_file", type=str, default=None, help="可选，待推理问题 jsonl 文件。")
    parser.add_argument("--output_file", type=str, default=None, help="可选，推理结果 jsonl 输出路径。")
    parser.add_argument("--system_prompt", type=str, default=DEFAULT_SYSTEM_PROMPT, help="推理时使用的 system prompt。")
    parser.add_argument("--max_new_tokens", type=int, default=1024, help="最大生成 token 数。")
    parser.add_argument("--temperature", type=float, default=0.7, help="采样温度，仅在 --do_sample 时生效。")
    parser.add_argument("--top_p", type=float, default=0.9, help="top-p 采样参数，仅在 --do_sample 时生效。")
    parser.add_argument("--do_sample", action="store_true", help="开启采样；默认关闭，便于稳定评估。")
    parser.add_argument("--merge_lora", action="store_true", help="将 LoRA 合并进基础模型后推理。")
    return parser.parse_args()


def load_questions(path: str | None) -> list[dict[str, Any]]:
    if path is None:
        return [{"id": f"demo_{idx + 1}", "question": question} for idx, question in enumerate(DEFAULT_QUESTIONS)]

    records = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line_idx, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            question = item.get("question")
            if not question and item.get("messages"):
                for message in item["messages"]:
                    if message.get("role") == "user":
                        question = message.get("content")
                        break
            if not question:
                raise ValueError(f"{path} 第 {line_idx} 行缺少 question 字段，也无法从 messages 中提取 user 内容。")

            records.append(
                {
                    "id": item.get("id", f"sample_{line_idx}"),
                    "question": question,
                    "standard_answer": item.get("standard_answer"),
                    "source": item.get("source"),
                }
            )
    return records


def check_format(response: str) -> dict[str, Any]:
    has_think_open = "<think>" in response
    has_think_close = "</think>" in response
    has_final_prefix = "最终答案：" in response or "最终答案:" in response
    think_match = re.search(r"<think>(.*?)</think>", response, flags=re.DOTALL | re.IGNORECASE)
    final_match = re.search(r"最终答案[:：]\s*(.+)", response, flags=re.DOTALL)
    final_answer = final_match.group(1).strip() if final_match else ""

    return {
        "has_think_open": has_think_open,
        "has_think_close": has_think_close,
        "has_think_block": think_match is not None,
        "has_final_prefix": has_final_prefix,
        "final_answer": final_answer,
        "think_chars": len(think_match.group(1).strip()) if think_match else 0,
    }


def generate_one(model: Any, tokenizer: Any, system_prompt: str, question: str, args: argparse.Namespace) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer([prompt], return_tensors="pt").to(model.device)

    generation_kwargs = {
        "max_new_tokens": args.max_new_tokens,
        "do_sample": args.do_sample,
        "pad_token_id": tokenizer.eos_token_id,
        "eos_token_id": tokenizer.eos_token_id,
    }
    if args.do_sample:
        generation_kwargs.update({"temperature": args.temperature, "top_p": args.top_p})

    with torch.no_grad():
        outputs = model.generate(**inputs, **generation_kwargs)

    generated_ids = outputs[0][inputs.input_ids.shape[1] :]
    return tokenizer.decode(generated_ids, skip_special_tokens=True).strip()


def main() -> None:
    args = parse_args()

    print(f"加载 tokenizer：{args.base_model}")
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
        tokenizer.pad_token_id = tokenizer.eos_token_id

    print(f"加载基础模型：{args.base_model}")
    base_model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
    )

    print(f"加载 LoRA adapter：{args.lora_path}")
    model = PeftModel.from_pretrained(base_model, args.lora_path)
    if args.merge_lora:
        print("合并 LoRA adapter 后进行推理。")
        model = model.merge_and_unload()
    model.eval()

    records = load_questions(args.questions_file)
    print(f"开始推理，共 {len(records)} 道题。采样模式：{args.do_sample}")

    output_handle = None
    if args.output_file:
        output_path = Path(args.output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_handle = output_path.open("w", encoding="utf-8")

    results = []
    try:
        for idx, record in enumerate(tqdm(records, desc="推理进度"), start=1):
            response = generate_one(model, tokenizer, args.system_prompt, record["question"], args)
            fmt = check_format(response)
            result = {
                **record,
                "response": response,
                **fmt,
            }
            results.append(result)
            if output_handle:
                output_handle.write(json.dumps(result, ensure_ascii=False) + "\n")

            print("\n" + "=" * 60)
            print(f"测试问题 {idx}: {record['question']}")
            print("-" * 60)
            print(response)
    finally:
        if output_handle:
            output_handle.close()

    total = len(results)
    if total == 0:
        print("没有可推理的问题。")
        return

    think_block_count = sum(item["has_think_block"] for item in results)
    final_prefix_count = sum(item["has_final_prefix"] for item in results)
    full_format_count = sum(item["has_think_block"] and item["has_final_prefix"] for item in results)

    print("\n" + "=" * 60)
    print("格式统计")
    print("=" * 60)
    print(f"样本数：{total}")
    print(f"<think>...</think> 命中：{think_block_count}/{total} ({think_block_count / total:.2%})")
    print(f"最终答案前缀命中：{final_prefix_count}/{total} ({final_prefix_count / total:.2%})")
    print(f"完整格式命中：{full_format_count}/{total} ({full_format_count / total:.2%})")
    if args.output_file:
        print(f"推理结果已保存至：{args.output_file}")


if __name__ == "__main__":
    main()
