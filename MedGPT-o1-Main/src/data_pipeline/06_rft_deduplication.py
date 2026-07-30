import json
import os
import random
from collections import defaultdict
from tqdm import tqdm

# Settings
RFT_PASS_FILE = "data/intermediate/rft_full_v1/rft_strict_pass.jsonl"
SFT_V1_FILE = "data/final/sft/sft_all.jsonl"

SFT_V2_DIR = "data/final/sft_v2"
SFT_V2_ALL = os.path.join(SFT_V2_DIR, "sft_v2_all.jsonl")
SFT_V2_TRAIN = os.path.join(SFT_V2_DIR, "sft_v2_train.jsonl")
SFT_V2_VAL = os.path.join(SFT_V2_DIR, "sft_v2_val.jsonl")

REPORT_FILE = "reports/rft_human_review_sample.md"

# Random seed for train/val split reproducibility and sampling
random.seed(42)

def main():
    os.makedirs(SFT_V2_DIR, exist_ok=True)
    os.makedirs("reports", exist_ok=True)

    print(f"Loading RFT candidates from {RFT_PASS_FILE}...")
    rft_candidates = defaultdict(list)
    rft_records = []
    
    with open(RFT_PASS_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip(): continue
            data = json.loads(line)
            # RFT's question_id maps exactly to SFT's openqa_id
            qid = data["question_id"]
            rft_candidates[qid].append(data)
            rft_records.append(data)

    print(f"Loaded {len(rft_records)} RFT candidates covering {len(rft_candidates)} unique questions.")

    # 1. Rank and Select Best RFT Candidate per question
    best_rft_map = {}
    source_distribution = defaultdict(int)
    
    for qid, candidates in rft_candidates.items():
        best_candidate = None
        best_rank = None
        
        for cand in candidates:
            step_count = cand.get("quality_step_count", 0)
            think_chars = cand.get("quality_think_chars", 0)
            cand_index = cand.get("candidate_index", 0)
            
            eligible = (step_count >= 3) and (think_chars >= 120)
            
            # Tuple ranking logic
            # Higher tuple wins:
            # 1. eligible (True > False, i.e., 1 > 0)
            # 2. -abs(think_chars - 220) (closer to 220 is higher)
            # 3. -think_chars (shorter is better among same distance)
            # 4. -step_count (fewer steps better among same length, preventing overly fragmented)
            # 5. -cand_index (stable fallback)
            rank = (
                int(eligible),
                -abs(think_chars - 220),
                -think_chars,
                -step_count,
                -cand_index
            )
            
            if best_rank is None or rank > best_rank:
                best_rank = rank
                best_candidate = cand
        
        best_rft_map[qid] = best_candidate
        source_distribution[best_candidate.get("source", "unknown")] += 1

    print(f"\nSelected {len(best_rft_map)} best RFT CoTs.")
    print("Source distribution of RFT best candidates:")
    for src, count in source_distribution.items():
        print(f"  - {src}: {count}")

    # 1.5 Export best candidates for auditing
    best_export_file = "data/intermediate/rft_full_v1/rft_best_per_question.jsonl"
    print(f"\nExporting {len(best_rft_map)} best RFT candidates to {best_export_file}...")
    os.makedirs(os.path.dirname(best_export_file), exist_ok=True)
    with open(best_export_file, 'w', encoding='utf-8') as f:
        for cand in best_rft_map.values():
            f.write(json.dumps(cand, ensure_ascii=False) + '\n')

    # 2. Additive Merge with SFT-v1 (KEEP BOTH)
    print(f"\nLoading original SFT from {SFT_V1_FILE}...")
    sft_v2_records = []
    unique_questions = set()
    
    with open(SFT_V1_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip(): continue
            data = json.loads(line)
            qid = data.get("openqa_id")
            sft_v2_records.append(data)
            unique_questions.add(qid)
            
    original_sft_count = len(sft_v2_records)
    print(f"Loaded {original_sft_count} original SFT records.")

    # Append Best RFT candidates
    rft_added = 0
    new_questions = 0
    
    for qid, rft_cand in best_rft_map.items():
        # Construct SFT format
        sft_record = {
            "id": f"sft_{qid}_rft",
            "source_cot_id": rft_cand["id"],
            "openqa_id": qid,
            "source_id": rft_cand["source_id"],
            "source": rft_cand["source"],
            "split": "train", # will re-split later
            "question": rft_cand["question"],
            "standard_answer": rft_cand["standard_answer"],
            "answer_aliases": rft_cand.get("answer_aliases", []),
            "quality_score": rft_cand.get("quality_score", 100.0),
            "quality_step_count": rft_cand.get("quality_step_count", 0),
            "quality_think_chars": rft_cand.get("quality_think_chars", 0),
            "messages": [
                {
                    "role": "system",
                    "content": "你是一个严谨的中文医学推理助手。请基于题干进行医学推理，并给出最终答案。"
                },
                {
                    "role": "user",
                    "content": rft_cand["question"]
                },
                {
                    "role": "assistant",
                    "content": rft_cand["cot_content"]
                }
            ]
        }
        
        sft_v2_records.append(sft_record)
        rft_added += 1
        if qid not in unique_questions:
            unique_questions.add(qid)
            new_questions += 1

    print(f"Merged RFT data: Added {rft_added} RFT records. New questions covered: {new_questions}.")
    print(f"Total SFT-v2 records: {len(sft_v2_records)}")
    print(f"Total SFT-v2 unique questions: {len(unique_questions)}")

    # 3. Train/Val Split (Group by openqa_id to prevent leakage)
    print("\nShuffling and splitting 90/10 by unique question...")
    
    # Group records by question ID
    grouped_records = defaultdict(list)
    for r in sft_v2_records:
        grouped_records[r["openqa_id"]].append(r)
        
    unique_qids = list(grouped_records.keys())
    unique_qids.sort() # Ensure deterministic shuffle
    random.shuffle(unique_qids)
    
    val_qids_size = int(len(unique_qids) * 0.1)
    val_qids = set(unique_qids[:val_qids_size])
    
    train_records = []
    val_records = []
    
    for qid, records in grouped_records.items():
        if qid in val_qids:
            for r in records:
                r["split"] = "val"
                val_records.append(r)
        else:
            for r in records:
                r["split"] = "train"
                train_records.append(r)
                
    all_records = train_records + val_records

    # Write SFT-v2 files
    print(f"Writing to {SFT_V2_DIR}...")
    def write_jsonl(filepath, records):
        with open(filepath, 'w', encoding='utf-8') as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')

    write_jsonl(SFT_V2_ALL, all_records)
    write_jsonl(SFT_V2_TRAIN, train_records)
    write_jsonl(SFT_V2_VAL, val_records)
    
    print(f"  - Train: {len(train_records)}")
    print(f"  - Val:   {len(val_records)}")
    print(f"  - All:   {len(all_records)}")

    # 4. Human Review Sampling
    print(f"\nGenerating Human Review Sample to {REPORT_FILE}...")
    review_sources = ["medqa_zh", "cmexam"]
    samples_per_source = 40
    
    # Collect pool from best RFT candidates (only what we actually use in SFT-v2)
    pool = defaultdict(list)
    for cand in best_rft_map.values():
        pool[cand.get("source", "unknown")].append(cand)
        
    with open(REPORT_FILE, 'w', encoding='utf-8') as f:
        f.write("# RFT-SFT-v2 Human Review Sample\n\n")
        f.write("This document contains a random sample of strictly passed RFT CoTs that were selected as the BEST candidate for their question. ")
        f.write("The goal is to manually verify the medical rigor and logical flow of the generated reasoning steps.\n\n")
        
        for src in review_sources:
            if src not in pool:
                continue
            src_pool = pool[src]
            sample_size = min(samples_per_source, len(src_pool))
            selected = random.sample(src_pool, sample_size)
            
            f.write(f"## Source: {src.upper()} (Sampled {sample_size} records)\n\n")
            
            for i, cand in enumerate(selected):
                f.write(f"### Sample {i+1} / {sample_size}\n")
                f.write(f"**Question ID:** `{cand['question_id']}` | **Source CoT ID:** `{cand['id']}`\n")
                f.write(f"**Candidate Index:** {cand.get('candidate_index')} | **Step Count:** {cand.get('quality_step_count')} | **Think Chars:** {cand.get('quality_think_chars')}\n\n")
                f.write(f"**Question:**\n{cand['question']}\n\n")
                f.write(f"**Standard Answer:**\n{cand['standard_answer']}\n\n")
                f.write(f"**CoT Content:**\n```text\n{cand['cot_content']}\n```\n\n")
                f.write("**Review:**\n")
                f.write("- [ ] Pass\n")
                f.write("- [ ] Minor issue\n")
                f.write("- [ ] Reject\n")
                f.write("- Notes: \n\n")
                f.write("---\n\n")
                
    print("Done! SFT-v2 construction complete.")

if __name__ == "__main__":
    main()
