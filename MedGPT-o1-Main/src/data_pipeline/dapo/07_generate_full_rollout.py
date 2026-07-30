#!/usr/bin/env python3
"""
Generate a full rollout dataset using vLLM for DAPO-lite filtering.

This script loads the training dataset, generates `num_generations` completions for each prompt
using the specified model via vLLM, and scores them using the `composite_reward_v3_func`.
The output JSONL can be passed to `06_build_dapo_effective_dataset.py` to extract the effective samples.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from collections import defaultdict

from datasets import load_dataset
from vllm import LLM, SamplingParams

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.rewards.composite_reward import composite_reward_v3_func
from src.rewards.hard_constraints import check_exact_match, extract_final_answer


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_path", type=str, required=True, help="Path to the merged SFT model.")
    parser.add_argument("--input_file", type=str, default="data/final/rl_v3/rl_clean_train.jsonl")
    parser.add_argument("--output_file", type=str, default="reports/grpo_full_rollout_train.jsonl")
    parser.add_argument("--max_samples", type=int, default=5000, help="Max number of prompts to process.")
    parser.add_argument("--num_generations", type=int, default=4, help="Number of generations per prompt.")
    parser.add_argument("--max_tokens", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--top_p", type=float, default=0.9)
    parser.add_argument("--vllm_gpu_memory_utilization", type=float, default=0.85, help="Maximize this for 80G cards")
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def load_completed_ids_and_compact_output(out_path: Path, expected_generations: int) -> set[str]:
    """Return fully processed prompt ids and remove incomplete checkpoint groups."""

    if not out_path.exists():
        return set()

    rows_by_id = defaultdict(list)
    with open(out_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            prompt_id = str(row.get("id") or "").strip()
            if prompt_id:
                rows_by_id[prompt_id].append(row)

    completed_ids = set()
    kept_rows = []
    dropped_ids = []
    expected_indices = set(range(expected_generations))
    for prompt_id, rows in rows_by_id.items():
        indices = {row.get("candidate_index") for row in rows}
        if expected_indices.issubset(indices):
            completed_ids.add(prompt_id)
            seen = set()
            for row in sorted(rows, key=lambda item: item.get("candidate_index", 0)):
                idx = row.get("candidate_index")
                if idx in expected_indices and idx not in seen:
                    kept_rows.append(row)
                    seen.add(idx)
        else:
            dropped_ids.append(prompt_id)

    if dropped_ids or sum(len(rows) for rows in rows_by_id.values()) != len(kept_rows):
        backup_path = out_path.with_suffix(out_path.suffix + ".bak")
        if backup_path.exists():
            backup_path.unlink()
        out_path.rename(backup_path)
        with open(out_path, "w", encoding="utf-8") as f:
            for row in kept_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(
            f"Compacted checkpoint: kept {len(completed_ids)} complete prompts, "
            f"dropped {len(dropped_ids)} incomplete prompts. Backup: {backup_path}"
        )

    return completed_ids


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
    
    # 1. Load already processed IDs to support Resume/Checkpoint
    processed_ids = set()
    out_path = Path(args.output_file)
    if out_path.exists():
        processed_ids = load_completed_ids_and_compact_output(out_path, args.num_generations)
        print(f"Found {len(processed_ids)} fully processed prompts in {args.output_file}. They will be skipped.")

    # 2. Filter dataset
    original_len = len(dataset)
    dataset = dataset.filter(lambda x: x.get("id") not in processed_ids)
    print(f"Total prompts to process after filtering: {len(dataset)} (Skipped {original_len - len(dataset)})")
    
    if len(dataset) == 0:
        print("All prompts are already processed. Exiting.")
        return

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
    
    print("Generating completions...")
    outputs = llm.generate(prompts, sampling_params)
    
    print("Scoring completions and saving to file incrementally...")
    
    # Open file in append mode for streaming writes
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as f:
        # Because MiMo Judge has rate limits, we score them sequentially or with low concurrency.
        # composite_reward_v3_func already handles concurrency for API calls.
        for i, output in enumerate(outputs):
            sample = dataset[i]
            prompt_id = sample.get("id", str(i))
            question = sample.get("question", "")
            standard_answer = sample.get("standard_answer", "")
            answer_aliases = sample.get("answer_aliases", [])
            
            completions = [out.text.strip() for out in output.outputs]
            
            # composite_reward_v3_func expects lists
            rewards_v3 = composite_reward_v3_func(
                completions=completions,
                standard_answer=[standard_answer] * len(completions),
                answer_aliases=[answer_aliases] * len(completions),
                question=[question] * len(completions)
            )
            
            for gen_idx, (response, score) in enumerate(zip(completions, rewards_v3)):
                extracted = extract_final_answer(response)
                match_type = "exact" if check_exact_match(extracted, standard_answer, answer_aliases) else "none"
                
                record = {
                    "id": prompt_id,
                    "question": question,
                    "standard_answer": standard_answer,
                    "answer_aliases": answer_aliases,
                    "candidate_index": gen_idx,
                    "response": response,
                    "match_type": match_type,
                    "v3_reward": score,
                    "total_reward": score,
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            
            # Flush immediately to save progress for this prompt
            f.flush()
            
    print("Rollout generation complete!")


if __name__ == "__main__":
    main()
