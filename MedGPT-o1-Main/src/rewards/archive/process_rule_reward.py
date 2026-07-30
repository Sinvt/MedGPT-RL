import re
from src.rewards.archive.orm_reward import completion_to_text


def length_reward_func(completions, **kwargs):
    """轻量过程奖励：鼓励必要推理，避免无节制长推理。"""
    rewards = []
    for completion in completions:
        text = completion_to_text(completion)
        match = re.search(r"<think>(.*?)</think>", text, flags=re.DOTALL | re.IGNORECASE)
        if not match:
            rewards.append(0.0)
            continue
        think_len = len(match.group(1).strip())
        if 80 <= think_len <= 800:
            rewards.append(0.4)
        elif 40 <= think_len < 80 or 800 < think_len <= 1200:
            rewards.append(0.2)
        else:
            rewards.append(0.0)
    return rewards


def repetition_penalty_reward_func(completions, **kwargs):
    """复读机惩罚：按标点断句检测长句重复，最高惩罚上限 -0.25。"""
    rewards = []
    for completion in completions:
        text = completion_to_text(completion)
        match = re.search(r"<think>(.*?)</think>", text, flags=re.DOTALL | re.IGNORECASE)
        if not match:
            rewards.append(0.0)
            continue
        think_text = match.group(1).strip()
        if not think_text:
            rewards.append(0.0)
            continue
        
        # 将文本按标点符号切分为片段
        segments = [s.strip() for s in re.split(r"[。，！？；\n,.;!?]", think_text) if s.strip()]
        if not segments:
            rewards.append(0.0)
            continue
            
        # 统计长度大于5个字符的片段的重复次数
        segment_counts = {}
        for seg in segments:
            if len(seg) > 5:
                segment_counts[seg] = segment_counts.get(seg, 0) + 1
                
        penalty = 0.0
        for count in segment_counts.values():
            if count >= 3: # 同样的一句话重复3次以上，开始扣分
                penalty -= 0.1 * (count - 2)
                
        # 严格封顶在 -0.25
        rewards.append(max(-0.25, penalty))
    return rewards
