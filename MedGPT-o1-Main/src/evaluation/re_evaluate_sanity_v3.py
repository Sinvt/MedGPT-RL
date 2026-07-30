import json
import os
import time
import sys
import pathlib
from collections import defaultdict

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from src.rewards.composite_reward import composite_reward_v3_func, get_mimo_judge
from src.rewards.hard_constraints import extract_final_answer

def main():
    input_file = "reports/grpo_val_infer.jsonl"
    output_file = "reports/grpo_val_infer_scored.jsonl"
    
    if not os.path.exists(input_file):
        print(f"找不到输入文件: {input_file}。请确保已经在云端把文件拉下来了。")
        return
        
    records = []
    groups = defaultdict(list)
    
    with open(input_file, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            records.append(rec)
            groups[rec["question"]].append(rec)
            
    print(f"加载了 {len(records)} 条候选回答，归属 {len(groups)} 个问题组。")
    print("开始调用 V3 复合奖励进行重打分...")
    
    judge = get_mimo_judge()
    
    total_latency = 0.0
    cache_hits = 0
    total_calls = 0
    judge_scores = []
    match_types = {"exact": 0, "contain": 0, "none": 0}
    
    re_eval_records = []
    
    for i, rec in enumerate(records):
        print(f"处理 {i+1}/{len(records)}...", end="\r")
        completions = [rec["response"]]
        standard_answer = [rec.get("standard_answer", "")]
        answer_aliases = [rec.get("answer_aliases", [])]
        question = [rec.get("question", "")]
        
        # 记录调用前的缓存状态
        final_ans = extract_final_answer(rec["response"])
        
        # 调用 V3
        reward_val = composite_reward_v3_func(completions, standard_answer, answer_aliases, question=question)[0]
        
        # 探测是否触发了 Judge
        # 由于我们没有直接访问 judge 的返回结构，我们可以查一次缓存
        # 实际上我们在 composite_reward 里调用了 judge.evaluate，它的结果被缓存了
        # 为了统计数据，我们手动取一下缓存里的最新结果
        # 但要注意，如果 exact_match，根本就不会进 Judge
        
        match_t = rec.get("match_type", "none")
        if match_t == "contain_pred" or match_t == "contain_resp":
            match_types["contain"] += 1
        else:
            match_types[match_t] += 1
            
        j_score = None
        has_contradiction = False
        cached = False
        latency = 0.0
        
        # 简单粗暴的判断：如果是 exact 或者 格式错误(-0.25)，没进 API
        if reward_val != 2.15 and reward_val != -0.25:
            # 进了 API
            # 我们直接从 judge cache 读
            # 这里重构：直接调用 evaluate(读取缓存) 并不耗时
            pred = extract_final_answer(rec["response"])
            pred_clean = pred if pred else "无答案"
            res = judge.evaluate(question[0], standard_answer[0], pred_clean)
            j_score = res.get("semantic_score", 0.0)
            has_contradiction = res.get("has_medical_contradiction", False)
            cached = res.get("cached", True)
            latency = res.get("latency", 0.0)
            
            judge_scores.append(j_score)
            total_calls += 1
            if cached:
                cache_hits += 1
            else:
                total_latency += latency
                
        rec["v3_reward"] = reward_val
        rec["judge_info"] = {
            "semantic_score": j_score,
            "has_contradiction": has_contradiction
        }
        re_eval_records.append(rec)
        
    print("\n重打分完成！开始统计...")
    
    # 统计组内差异
    diff_groups = 0
    for q, grp in groups.items():
        scores = set([r.get("v3_reward", 0.0) for r in grp])
        if len(scores) > 1:
            diff_groups += 1
            
    # 统计 Judge 分布
    high = sum(1 for s in judge_scores if s >= 0.8)
    mid = sum(1 for s in judge_scores if 0.3 <= s < 0.8)
    low = sum(1 for s in judge_scores if s < 0.3)
    
    print("="*60)
    print("V3 复合奖励重打分报告")
    print("="*60)
    print(f"总组数: {len(groups)}")
    print(f"有奖励差异的组数: {diff_groups} / {len(groups)}")
    print("-" * 60)
    print(f"Match 类别分布: Exact: {match_types['exact']}, Contain: {match_types['contain']}, None: {match_types['none']}")
    print("-" * 60)
    print(f"Judge 触发总次数: {total_calls}")
    if total_calls > 0:
        print(f"  - High (>=0.8): {high} ({high/total_calls:.1%})")
        print(f"  - Mid (0.3-0.8): {mid} ({mid/total_calls:.1%})")
        print(f"  - Low (<0.3): {low} ({low/total_calls:.1%})")
    print("-" * 60)
    print(f"API 缓存命中: {cache_hits} / {total_calls}")
    actual_api_calls = total_calls - cache_hits
    avg_lat = total_latency / actual_api_calls if actual_api_calls > 0 else 0
    print(f"平均 API 延迟: {avg_lat:.2f} 秒")
    print("="*60)
    
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        for r in re_eval_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            
    print(f"详细打分结果已保存至 {output_file}。请抽查 J > 0.8 和 J < 0.3 的 non-exact 样本。")

if __name__ == "__main__":
    main()
