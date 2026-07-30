#!/usr/bin/env python3
"""
Score rollout texts ONLY using CPU (API calls).
This script reads the generated cache from the GPU step and sends them to MiMo Judge.
It supports Checkpoint/Resume out of the box.
"""

import argparse
import json
import sys
from pathlib import Path
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.rewards.composite_reward import composite_reward_v3_func
from src.rewards.hard_constraints import check_exact_match, extract_final_answer

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_file", type=str, default="reports/dapo/rollout_texts_cache.jsonl")
    parser.add_argument("--output_file", type=str, default="reports/dapo/sft_v2_a_full_rollout_for_dapo.jsonl")
    parser.add_argument("--max_samples", type=int, default=-1, help="Max prompts to score (-1 for all).")
    parser.add_argument("--expected_generations", type=int, default=4, help="Expected candidates per prompt.")
    return parser.parse_args()


def load_completed_ids_and_compact_output(out_path: Path, expected_generations: int) -> set[str]:
    """Return fully scored prompt ids and remove incomplete checkpoint groups.

    A previous run can be interrupted while writing candidate rows for one prompt.
    Treating any seen id as complete would silently drop that prompt from resume.
    """

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
            # Keep one row per candidate index, preserving the first complete set.
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


def main():
    args = parse_args()
    
    in_path = Path(args.input_file)
    if not in_path.exists():
        print(f"Error: {args.input_file} not found. Please run 07_a_gpu_generate_only.py first.")
        return
        
    print(f"Loading generated texts from {args.input_file}...")
    
    # 1. Load generated cache and group by prompt ID
    grouped_prompts = defaultdict(list)
    with open(in_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip(): continue
            try:
                record = json.loads(line)
                pid = record.get("id")
                grouped_prompts[pid].append(record)
            except:
                pass
                
    prompt_ids = list(grouped_prompts.keys())
    print(f"Found {len(prompt_ids)} total generated prompts in cache.")
    
    # 2. Checkpoint/Resume logic
    out_path = Path(args.output_file)
    processed_ids = load_completed_ids_and_compact_output(out_path, args.expected_generations)
    print(f"Found {len(processed_ids)} fully processed prompts in {args.output_file}.")
        
    # Filter out already processed
    pending_ids = [pid for pid in prompt_ids if pid not in processed_ids]
    if args.max_samples > 0:
        pending_ids = pending_ids[:args.max_samples]
        
    print(f"Pending prompts to score: {len(pending_ids)}")
    if len(pending_ids) == 0:
        print("All prompts are already scored. Exiting.")
        return
        
    print("Scoring completions and saving incrementally...")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Process sequentially prompt by prompt
    with open(out_path, "a", encoding="utf-8") as f:
        for pid in pending_ids:
            candidates = grouped_prompts[pid]
            if not candidates: continue
            
            question = candidates[0].get("question", "")
            standard_answer = candidates[0].get("standard_answer", "")
            answer_aliases = candidates[0].get("answer_aliases", [])
            
            # Sort by candidate_index to ensure order
            candidates = sorted(candidates, key=lambda x: x.get("candidate_index", 0))
            completions = [c.get("response", "") for c in candidates]
            
            # Batch score the candidates for this prompt
            rewards_v3 = composite_reward_v3_func(
                completions=completions,
                standard_answer=[standard_answer] * len(completions),
                answer_aliases=[answer_aliases] * len(completions),
                question=[question] * len(completions)
            )
            
            for gen_idx, (c, score) in enumerate(zip(candidates, rewards_v3)):
                response = c.get("response", "")
                extracted = extract_final_answer(response)
                match_type = "exact" if check_exact_match(extracted, standard_answer, answer_aliases) else "none"
                
                record = {
                    "id": pid,
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
                
            f.flush()
            print(f"Scored and saved prompt: {pid}")

    print("CPU Scoring Stage Complete!")


if __name__ == "__main__":
    main()
