import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

PUNCT_TRANSLATION = str.maketrans(
    {
        "，": "",
        ",": "",
        "。": "",
        ".": "",
        "；": "",
        ";": "",
        "：": "",
        ":": "",
        "！": "",
        "!": "",
        "？": "",
        "?": "",
        "（": "",
        "）": "",
        "(": "",
        ")": "",
        "【": "",
        "】": "",
        "[": "",
        "]": "",
        "\"": "",
        "'": "",
        "\u201c": "",
        "\u201d": "",
        "\u2018": "",
        "\u2019": "",
        " ": "",
        "\t": "",
        "\n": "",
        "\r": "",
    }
)


def completion_to_text(completion: Any) -> str:
    """兼容 TRL 可能返回的字符串、messages 或 dict completion。"""
    if isinstance(completion, str):
        return completion
    if isinstance(completion, list):
        if completion and isinstance(completion[-1], dict):
            return str(completion[-1].get("content", ""))
        return "\n".join(completion_to_text(item) for item in completion)
    if isinstance(completion, dict):
        return str(completion.get("content", ""))
    return str(completion or "")


def normalize_text(text: str) -> str:
    """用于医学短答案匹配的轻量标准化。"""
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = text.strip().lower()
    text = re.sub(r"\s+", "", text)
    return text.translate(PUNCT_TRANSLATION)


def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.DOTALL | re.IGNORECASE).strip()


def clean_answer(text: str) -> str:
    text = str(text or "").strip()
    text = re.sub(r"</?think>", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^(最终答案|答案|结论|诊断)\s*[:：]\s*", "", text).strip()
    text = re.split(r"[。；;\n]", text, maxsplit=1)[0].strip()
    return text


def extract_final_answer(response: str) -> str:
    """优先从非 think 区域抽取最终答案；抽不到时回退到最后一行。"""
    response = strip_think(response)
    patterns = [
        r"最终答案\s*[:：]\s*(.+)",
        r"答案\s*[:：]\s*(.+)",
        r"结论\s*[:：]\s*(.+)",
        r"诊断\s*[:：]\s*(.+)",
    ]
    for pattern in patterns:
        matches = re.findall(pattern, response, flags=re.DOTALL)
        if matches:
            return clean_answer(matches[-1])

    lines = [line.strip() for line in response.splitlines() if line.strip()]
    if lines:
        return clean_answer(lines[-1])
    return clean_answer(response)


def iter_answer_candidates(standard_answer: str, answer_aliases: Iterable[str] | None = None) -> list[str]:
    candidates = [str(standard_answer or "").strip()]
    if answer_aliases:
        candidates.extend(str(alias or "").strip() for alias in answer_aliases)

    seen = set()
    unique = []
    for candidate in candidates:
        normalized = normalize_text(candidate)
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(candidate)
    return unique


def is_safe_containment_answer(normalized_answer: str) -> bool:
    unsafe_short_answers = {"有", "无", "是", "否", "对", "错", "能", "正常", "异常"}
    return len(normalized_answer) >= 2 and normalized_answer not in unsafe_short_answers


# ---------------------------------------------------------------------------
# 结构化 ORM 结果
# ---------------------------------------------------------------------------


@dataclass
class OrmResult:
    """ORM 评分的完整结构化结果。"""
    score: float
    matched: bool
    match_type: str          # "exact" | "contain_pred" | "contain_resp" | "none"
    predicted_answer: str
    matched_answer: str


def score_response(
    response: str,
    standard_answer: str,
    answer_aliases: Iterable[str] | None = None,
) -> OrmResult:
    """医学 ORM：只在非 think 区域匹配最终答案，返回结构化结果。

    match_type 含义：
      - "exact":        预测答案与标准答案精确匹配（归一化后相等），得分 1.0
      - "contain_pred": 标准答案是预测答案的子串（安全长度），得分 0.9
      - "contain_resp": 标准答案出现在完整回复中（安全长度），得分 0.7
      - "none":         未命中任何标准答案或别名，得分 0.0
    """
    predicted_answer = extract_final_answer(response)
    normalized_prediction = normalize_text(predicted_answer)
    normalized_response = normalize_text(strip_think(response))

    for answer in iter_answer_candidates(standard_answer, answer_aliases):
        normalized_answer = normalize_text(answer)
        if not normalized_answer:
            continue

        # 精确匹配
        if normalized_prediction == normalized_answer:
            return OrmResult(
                score=1.0,
                matched=True,
                match_type="exact",
                predicted_answer=predicted_answer,
                matched_answer=answer,
            )

        # 包含匹配（需通过安全检查）
        if is_safe_containment_answer(normalized_answer):
            if normalized_answer in normalized_prediction:
                return OrmResult(
                    score=0.9,
                    matched=True,
                    match_type="contain_pred",
                    predicted_answer=predicted_answer,
                    matched_answer=answer,
                )
            if normalized_answer in normalized_response:
                return OrmResult(
                    score=0.7,
                    matched=True,
                    match_type="contain_resp",
                    predicted_answer=predicted_answer,
                    matched_answer=answer,
                )

    return OrmResult(
        score=0.0,
        matched=False,
        match_type="none",
        predicted_answer=predicted_answer,
        matched_answer="",
    )


def medical_orm_score(response: str, standard_answer: str, answer_aliases: Iterable[str] | None = None) -> float:
    """便捷接口：只返回浮点得分，供 GRPO 奖励函数等只需数值的场景使用。"""
    return score_response(response, standard_answer, answer_aliases).score


def accuracy_reward_func(completions, standard_answer, answer_aliases=None, **kwargs):
    """答案奖励：标准答案匹配成功最高给 2 分。"""
    if answer_aliases is None:
        answer_aliases = [None] * len(completions)

    rewards = []
    for completion, answer, aliases in zip(completions, standard_answer, answer_aliases):
        text = completion_to_text(completion)
        rewards.append(2.0 * medical_orm_score(text, answer, aliases))
    return rewards


def accuracy_reward_v2_func(completions, standard_answer, answer_aliases=None, **kwargs):
    """GRPO-v2 专属答案奖励：严格执行 exact_match -> 2.0，包含匹配 -> 0.0。"""
    if answer_aliases is None:
        answer_aliases = [None] * len(completions)

    rewards = []
    for completion, answer, aliases in zip(completions, standard_answer, answer_aliases):
        text = completion_to_text(completion)
        res = score_response(text, answer, aliases)
        if res.match_type == "exact":
            rewards.append(2.0)
        else:
            # contain_pred 和 contain_resp 直接给 0
            rewards.append(0.0)
    return rewards

