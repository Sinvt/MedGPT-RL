import re

def completion_to_text(completion) -> str:
    """提取生成对象的内容字符串。"""
    if isinstance(completion, list):
        return completion[0]["content"] if completion else ""
    elif isinstance(completion, dict):
        return completion.get("content", "")
    return str(completion)

def check_format(text: str) -> bool:
    """
    验证格式硬约束：
    1. 必须包含闭合的 <think>...</think>
    2. 在 </think> 之后必须有“最终答案：”前缀及非空内容
    """
    return bool(re.search(r"<think>.*?</think>.*?最终答案\s*[:：]\s*\S+", text, flags=re.DOTALL | re.IGNORECASE))

def extract_final_answer(text: str) -> str:
    """提取 '最终答案：' 之后的内容。"""
    match = re.search(r"最终答案\s*[:：]\s*(.*)", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ""

def normalize_text(text: str) -> str:
    """文本归一化，去除标点和空白，用于做严格比对。"""
    text = text.lower().strip()
    text = re.sub(r"[\s\n\r\t]+", "", text)
    text = re.sub(r"[.,?!;:。，？！；：、\(\)（）\[\]【】]", "", text)
    return text

def check_exact_match(predicted_answer: str, standard_answer: str, answer_aliases=None) -> bool:
    """
    检查预测答案与标准答案（及其别名）是否达到精确匹配 (Exact Match)。
    """
    if not predicted_answer:
        return False

    normalized_prediction = normalize_text(predicted_answer)
    
    # 1. 匹配标准答案
    if normalized_prediction == normalize_text(standard_answer):
        return True
        
    # 2. 匹配别名
    if not answer_aliases:
        return False
        
    if isinstance(answer_aliases, str):
        try:
            import ast
            aliases = ast.literal_eval(answer_aliases)
        except Exception:
            aliases = [answer_aliases]
    elif isinstance(answer_aliases, list):
        aliases = answer_aliases
    else:
        aliases = []
        
    for alias in aliases:
        if normalized_prediction == normalize_text(alias):
            return True
            
    return False
