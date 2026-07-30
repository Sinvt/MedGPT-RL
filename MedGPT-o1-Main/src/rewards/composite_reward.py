import concurrent.futures
import os
from src.rewards.hard_constraints import completion_to_text, check_format, extract_final_answer, check_exact_match
from src.rewards.llm_judge import MiMoJudge

# 全局初始化，复用缓存和 API Client
global_mimo_judge = None


def _judge_worker_count() -> int:
    """Keep API scoring conservative by default; allow an explicit override."""
    raw_value = os.environ.get("MIMO_JUDGE_MAX_WORKERS", "1")
    try:
        return max(1, int(raw_value))
    except ValueError:
        print("Warning: MIMO_JUDGE_MAX_WORKERS must be an integer; using 1.")
        return 1

def get_mimo_judge():
    global global_mimo_judge
    if global_mimo_judge is None:
        global_mimo_judge = MiMoJudge()
    return global_mimo_judge

def composite_reward_v3_func(completions, standard_answer, answer_aliases=None, question=None, prompts=None, **kwargs):
    """
    V3 终极复合奖励：硬格式约束 + Exact短路满分 + MiMo Judge 连续打分。
    Judge 并发默认保守为 1，并由 MiMoJudge 在请求层统一节流。
    """
    if answer_aliases is None:
        answer_aliases = [None] * len(completions)
    
    if question is None:
        question = ["" for _ in completions]
        if prompts is not None:
            for i, p in enumerate(prompts):
                if isinstance(p, list):
                    question[i] = p[-1]["content"] if p else ""
                else:
                    question[i] = p

    judge = get_mimo_judge()
    rewards = [None] * len(completions)
    
    # 收集需要发往 API 的任务 (拦截不合格和已 Exact 短路的)
    api_tasks = []
    
    for i, (completion, ans, aliases, q) in enumerate(zip(completions, standard_answer, answer_aliases, question)):
        text = completion_to_text(completion)
        
        # 1. 格式约束 (硬约束)
        if not check_format(text):
            rewards[i] = -0.25
            continue
            
        # 2. 精确命中 (Exact Match，硬约束短路)
        extracted_pred = extract_final_answer(text)
        if check_exact_match(extracted_pred, ans, aliases):
            rewards[i] = 2.15
            continue
            
        # 3. 差错拦截短路 (Heuristic Pre-filtering)
        # 如果模型输出了拒绝回答的字眼，或者答案极度离谱（比如过长），直接给 0分，不调 API
        refusal_keywords = ["无法确定", "无法判断", "不知道", "资料不足", "抱歉", "不对该问题进行", "无答案"]
        if any(kw in extracted_pred for kw in refusal_keywords) or len(extracted_pred) > 500:
            rewards[i] = 0.0
            continue
            
        # 4. 未被短路，准备走软裁判
        q_text = q if q else "未知问题"
        extracted_pred_clean = extracted_pred if extracted_pred else "无答案"
        api_tasks.append((i, q_text, ans, extracted_pred_clean))
        
    # 4. 并发调度软裁判
    if api_tasks:
        def _score_single(task):
            idx, q_t, a_t, p_t = task
            judge_res = judge.evaluate(q_t, a_t, p_t)
            if judge_res.get("has_medical_contradiction", False):
                return idx, 0.00
            else:
                j_score = float(judge_res.get("semantic_score", 0.0))
                j_score = max(0.0, min(1.0, j_score)) # clamp
                return idx, 0.15 + 1.70 * j_score

        # The provider can impose a QPS lower than two concurrent requests.
        # A single worker is the safe default; users may override it only after
        # verifying their own MiMo quota.
        with concurrent.futures.ThreadPoolExecutor(max_workers=_judge_worker_count()) as executor:
            future_to_task = {executor.submit(_score_single, t): t for t in api_tasks}
            failures = []
            for future in concurrent.futures.as_completed(future_to_task):
                try:
                    idx, score = future.result()
                    rewards[idx] = score
                except Exception as e:
                    failures.append((future_to_task[future][0], e))

            if failures:
                failed_indices = ", ".join(str(index) for index, _ in failures[:5])
                first_error = failures[0][1]
                raise RuntimeError(
                    "MiMo Judge failed after its bounded retries for "
                    f"{len(failures)} reward(s), including indices {failed_indices}. "
                    "Stopping instead of converting API failures into zero rewards. "
                    "Check MiMo rate limits and resume from the latest checkpoint."
                ) from first_error
                    
    return rewards
