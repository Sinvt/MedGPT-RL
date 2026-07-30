import os
import json
import collections
import random
from typing import List, Dict
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rewards.hard_constraints import normalize_text

def canonicalize_prompt(prompt_list: List[Dict]) -> str:
    """Normalize the prompt list into a string key."""
    # We can just dump it stably
    return json.dumps(prompt_list, ensure_ascii=False, separators=(',', ':'))

def canonicalize_answer(answer: str) -> str:
    """Normalize the standard answer."""
    return normalize_text(answer)

def main():
    random.seed(42)
    
    input_files = [
        "data/final/rl/rl_train.jsonl",
        "data/final/rl/rl_val.jsonl"
    ]
    
    out_dir = "data/final/rl_v3"
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs("reports", exist_ok=True)
    
    # 1. Load all data
    all_records = []
    for filepath in input_files:
        if not os.path.exists(filepath):
            print(f"Warning: {filepath} does not exist.")
            continue
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                all_records.append(json.loads(line))
                
    print(f"Total loaded records: {len(all_records)}")
    
    # 2. Group by canonical_prompt
    groups = collections.defaultdict(list)
    for rec in all_records:
        cp = canonicalize_prompt(rec["prompt"])
        rec["canonical_prompt"] = cp
        groups[cp].append(rec)
        
    print(f"Total unique canonical_prompts: {len(groups)}")
    
    # 3. Detect conflicts & safe duplicates
    clean_unique_records = []
    conflict_records = []
    manual_review_records = []
    manual_review_pairs = [] # To store pairs for the markdown report
    
    exact_duplicates_removed = 0
    conflict_groups_count = 0
    manual_review_groups_count = 0
    
    for cp, recs in groups.items():
        # Get all normalized standard answers for this prompt
        answers = set(canonicalize_answer(r["standard_answer"]) for r in recs)
        
        if len(answers) > 1:
            # Conflict! Same prompt, different standard answers
            if len(recs) <= 3 and len(answers) == 2:
                manual_review_groups_count += 1
                manual_review_records.extend(recs)
                # Save details for report
                q_text = recs[0].get("question", "Unknown")
                ans_list = list(answers)
                manual_review_pairs.append({
                    "question": q_text,
                    "answers": ans_list
                })
            else:
                conflict_groups_count += 1
                conflict_records.extend(recs)
        else:
            # No conflict. Keep exactly one copy (deduplication)
            clean_unique_records.append(recs[0])
            exact_duplicates_removed += len(recs) - 1
            
    print(f"Found {conflict_groups_count} major conflict groups ({len(conflict_records)} records).")
    print(f"Found {manual_review_groups_count} small conflict groups for manual review ({len(manual_review_records)} records).")
    print(f"Removed {exact_duplicates_removed} exact duplicates (same prompt, same answer).")
    print(f"Total clean unique records: {len(clean_unique_records)}")
    
    # 4. Stratified Split (90/10) by source
    source_to_recs = collections.defaultdict(list)
    for rec in clean_unique_records:
        source_to_recs[rec.get("source", "unknown")].append(rec)
        
    train_split = []
    val_split = []
    
    for src, recs in source_to_recs.items():
        random.shuffle(recs) # Shuffle within source
        split_idx = int(len(recs) * 0.9)
        train_split.extend(recs[:split_idx])
        val_split.extend(recs[split_idx:])
        
    # Final shuffle
    random.shuffle(train_split)
    random.shuffle(val_split)
    
    print(f"Train split size: {len(train_split)}")
    print(f"Val split size: {len(val_split)}")
    
    # 5. Assertions
    # 5.1 Same canonical prompt only has one answer
    final_groups = collections.defaultdict(set)
    for r in train_split + val_split:
        final_groups[r["canonical_prompt"]].add(canonicalize_answer(r["standard_answer"]))
    for cp, ans_set in final_groups.items():
        assert len(ans_set) == 1, f"Assertion failed: multiple answers for {cp}"
        
    # 5.2 Train and Val intersection is empty
    train_cps = set(r["canonical_prompt"] for r in train_split)
    val_cps = set(r["canonical_prompt"] for r in val_split)
    intersection = train_cps.intersection(val_cps)
    assert len(intersection) == 0, f"Assertion failed: train and val intersection is not empty! ({len(intersection)} overlaps)"
    
    print("All assertions passed safely!")
    
    # 6. Save outputs
    # helper for jsonl
    def save_jsonl(records, filename):
        with open(filename, "w", encoding="utf-8") as f:
            for r in records:
                # Remove canonical_prompt before saving to keep it clean, though harmless
                copy_r = {k:v for k,v in r.items() if k != "canonical_prompt"}
                f.write(json.dumps(copy_r, ensure_ascii=False) + "\n")
                
    save_jsonl(conflict_records, os.path.join(out_dir, "rl_conflict_isolated.jsonl"))
    save_jsonl(manual_review_records, os.path.join(out_dir, "rl_conflict_manual_review.jsonl"))
    save_jsonl(clean_unique_records, os.path.join(out_dir, "rl_clean_all.jsonl"))
    save_jsonl(train_split, os.path.join(out_dir, "rl_clean_train.jsonl"))
    save_jsonl(val_split, os.path.join(out_dir, "rl_clean_val.jsonl"))
    
    # 7. Audit Report
    audit_data = {
        "total_input_records": len(all_records),
        "total_unique_prompts": len(groups),
        "major_conflict_groups": conflict_groups_count,
        "major_conflict_records": len(conflict_records),
        "manual_review_groups": manual_review_groups_count,
        "manual_review_records": len(manual_review_records),
        "exact_duplicates_removed": exact_duplicates_removed,
        "final_clean_records": len(clean_unique_records),
        "train_size": len(train_split),
        "val_size": len(val_split),
        "assertions": {
            "single_answer_per_prompt": True,
            "train_val_mutually_exclusive": True
        }
    }
    
    with open("reports/grpo_data_clean_audit.json", "w", encoding="utf-8") as f:
        json.dump(audit_data, f, indent=4, ensure_ascii=False)
        
    with open("reports/grpo_data_clean_audit.md", "w", encoding="utf-8") as f:
        f.write("# GRPO Data Clean Audit Report\n\n")
        f.write(f"- **Total Input Records**: {audit_data['total_input_records']}\n")
        f.write(f"- **Major Conflict Groups Isolated**: {audit_data['major_conflict_groups']} (Total {audit_data['major_conflict_records']} records)\n")
        f.write(f"- **Small Conflicts for Manual Review**: {audit_data['manual_review_groups']} (Total {audit_data['manual_review_records']} records)\n")
        f.write(f"- **Exact Duplicates Removed**: {audit_data['exact_duplicates_removed']}\n")
        f.write(f"- **Final Clean Records**: {audit_data['final_clean_records']}\n")
        f.write(f"- **Train Split**: {audit_data['train_size']}\n")
        f.write(f"- **Val Split**: {audit_data['val_size']}\n")
        f.write("\n## Assertions\n")
        f.write("- `同 canonical_prompt 仅对应一个规范化答案`: **PASS**\n")
        f.write("- `train canonical_prompt ∩ val canonical_prompt = 空集`: **PASS**\n")
        f.write("\n## Manual Review Required\n")
        f.write("这些冲突组规模很小（仅 2 个不同答案），疑似标点符号/简称导致的不一致，或者需要人工添加 alias。在人工确认前，它们已被安全隔离，**没有进入训练集**。\n\n")
        for pair in manual_review_pairs:
            f.write(f"- **Q**: {pair['question']}\n")
            f.write(f"  - Ans 1: {pair['answers'][0]}\n")
            f.write(f"  - Ans 2: {pair['answers'][1]}\n\n")

    print(f"Data cleaning finished. Outputs saved to {out_dir}")

if __name__ == "__main__":
    main()
