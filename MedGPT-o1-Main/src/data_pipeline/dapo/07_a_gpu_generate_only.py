#!/usr/bin/env python3
"""
Generate rollout texts ONLY using vLLM (GPU intensive).
This script does NOT perform any API calls. It strictly uses the GPU to 
generate candidates and saves them. This allows the expensive GPU instance 
to be released immediately after generation finishes.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from datasets import load_dataset
from vllm import LLM, SamplingParams

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_path", type=str, required=True, help="Path to the merged SFT model.")
    parser.add_argument("--input_file", type=str, default="data/final/rl_v3/rl_clean_train.jsonl")
    parser.add_argument("--output_file", type=str, default="reports/dapo/rollout_texts_cache.jsonl")
    parser.add_argument("--max_samples", type=int, default=-1, help="Max number of prompts to process (-1 for all).")
    parser.add_argument("--num_generations", type=int, default=4, help="Number of generations per prompt.")
    parser.add_argument("--max_tokens", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--top_p", type=float, default=0.9)
    parser.add_argument("--vllm_gpu_memory_utilization", type=float, default=0.85, help="Maximize this for 80G/40G/24G cards")
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def render_prompt_value(sample: dict[str, Any]) -> str:
    question = sample.get("question", "")
    messages = [
        {
            "role": "system",
            "content": (
                "你是一个严谨的中文医学推理助手。请先在 <think>...</think> "
                "中写出必要推理过程，再用 `最终答案：` 给出简洁答案。"
            ),
        },
        {"role": "user", "content": str(question)},
    ]
    # vLLM prompt formatting
    prompt = f"<|im_start|>system\n{messages[0]['content']}<|im_end|>\n<|im_start|>user\n{messages[1]['content']}<|im_end|>\n<|im_start|>assistant\n"
    return prompt


def main():
    args = parse_args()
    
    print(f"Loading dataset from {args.input_file}...")
    dataset = load_dataset("json", data_files=args.input_file, split="train")
    if args.max_samples > 0:
        dataset = dataset.select(range(min(len(dataset), args.max_samples)))
    
    print(f"Total prompts to process: {len(dataset)}")
    
    # Check if target already exists to avoid accidental overwrite
    out_path = Path(args.output_file)
    if out_path.exists():
        print(f"WARNING: Output file {args.output_file} already exists. It will be OVERWRITTEN!")
    
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Initialize vLLM
    print(f"Loading model {args.model_path} into vLLM...")
    llm = LLM(
        model=args.model_path,
        trust_remote_code=True,
        tensor_parallel_size=1,
        gpu_memory_utilization=args.vllm_gpu_memory_utilization,
        seed=args.seed
    )
    
    sampling_params = SamplingParams(
        n=args.num_generations,
        temperature=args.temperature,
        top_p=args.top_p,
        max_tokens=args.max_tokens,
        seed=args.seed,
    )
    
    prompts = [render_prompt_value(sample) for sample in dataset]
    
    print(f"Generating {args.num_generations} completions per prompt (Total: {len(prompts) * args.num_generations})...")
    outputs = llm.generate(prompts, sampling_params)
    
    print("Generation complete! Saving purely text cache to file...")
    
    records = []
    for i, output in enumerate(outputs):
        sample = dataset[i]
        prompt_id = sample.get("id", str(i))
        question = sample.get("question", "")
        standard_answer = sample.get("standard_answer", "")
        answer_aliases = sample.get("answer_aliases", [])
        
        for gen_idx, out in enumerate(output.outputs):
            response = out.text.strip()
            record = {
                "id": prompt_id,
                "question": question,
                "standard_answer": standard_answer,
                "answer_aliases": answer_aliases,
                "candidate_index": gen_idx,
                "response": response,
                # match_type and rewards are intentionally omitted here.
            }
            records.append(record)
            
    print(f"Saving {len(records)} generated text records to {args.output_file}...")
    with open(out_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            
    print("GPU Stage Complete! You can now power down the GPU instance.")


if __name__ == "__main__":
    main()
