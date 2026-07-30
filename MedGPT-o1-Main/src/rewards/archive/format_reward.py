import re
from src.rewards.archive.orm_reward import completion_to_text


def format_reward_func(completions, **kwargs):
    """格式奖励：鼓励 <think> 推理区和“最终答案：”短答案区。"""
    rewards = []
    for completion in completions:
        text = completion_to_text(completion)
        score = 0.0
        if "<think>" in text:
            score += 0.2
        if "</think>" in text:
            score += 0.2
        if re.search(r"最终答案\s*[:：]", text):
            score += 0.4
        if re.search(r"<think>.*?</think>.*?最终答案\s*[:：]\s*\S+", text, flags=re.DOTALL | re.IGNORECASE):
            score += 0.7
        rewards.append(score)
    return rewards


def format_reward_v2_func(completions, **kwargs):
    """GRPO-v2 专属格式奖励：严格收紧权重，完整格式仅给 0.15 分。"""
    rewards = []
    for completion in completions:
        text = completion_to_text(completion)
        # 只有闭合的 think 区块后接非空最终答案才获得格式分。
        if re.search(r"<think>.*?</think>.*?最终答案\s*[:：]\s*\S+", text, flags=re.DOTALL | re.IGNORECASE):
            rewards.append(0.15)
        else:
            rewards.append(0.0)
    return rewards
