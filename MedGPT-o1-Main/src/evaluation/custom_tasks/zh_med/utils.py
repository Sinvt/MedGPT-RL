import re


LABELS = list("ABCDEFGHIJ")


def _clean(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(_clean(v) for v in value if v is not None).strip()
    return str(value).strip()


def _first(doc, keys):
    for key in keys:
        value = doc.get(key)
        if value not in (None, ""):
            return value
    return ""


def _normalize_choice_letters(text):
    upper_text = _clean(text).upper()
    if not re.fullmatch(r"[\sA-J,，、/|]+", upper_text):
        return ""
    return "".join(dict.fromkeys(re.findall(r"[A-J]", upper_text)))


def _parse_options_value(value):
    if not value:
        return {}

    if isinstance(value, dict):
        parsed = {}
        for key, text in value.items():
            label = _clean(key).upper()
            text = _clean(text)
            if label and text:
                parsed[label[0]] = text
        return parsed

    if isinstance(value, list):
        parsed = {}
        for idx, item in enumerate(value[: len(LABELS)]):
            label = LABELS[idx]
            text = ""
            if isinstance(item, dict):
                raw_label = _first(item, ["key", "label", "name", "option"])
                label = _clean(raw_label).upper()[:1] or label
                text = _clean(_first(item, ["value", "text", "content", "answer", "statement"]))
            else:
                text = _clean(item)
            if label and text:
                parsed[label] = text
        return parsed

    text = _clean(value)
    pattern = r"(?:^|\s)([A-J])[\.\．、:：]\s*(.*?)(?=\s+[A-J][\.\．、:：]\s*|$)"
    matches = re.findall(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    return {label.upper(): body.strip() for label, body in matches if body.strip()}


def _options_dict(doc):
    parsed = {}
    for label in LABELS:
        value = _first(
            doc,
            [
                f"option_{label}",
                f"Option_{label}",
                f"option{label}",
                f"Option{label}",
                f"choice_{label}",
                f"Choice_{label}",
                label,
                label.lower(),
            ],
        )
        if value:
            parsed[label] = _clean(value)

    if parsed:
        return parsed

    return _parse_options_value(
        _first(doc, ["options", "Options", "option", "Option", "choices", "Choices", "answer_choices"])
    )


def format_options(doc):
    options = _options_dict(doc)
    ordered = [(label, options[label]) for label in LABELS if label in options and options[label]]
    return [f"{label}. {text}" for label, text in ordered], [label for label, _ in ordered]


def doc_to_text(doc):
    question = _clean(_first(doc, ["question", "Question", "exam_question", "title", "query", "prompt", "stem"]))
    options, _ = format_options(doc)
    options_text = "\n".join(options)
    question_type = _clean(_first(doc, ["question_type", "QuestionType", "type"]))
    if "多" in question_type:
        instruction = (
            "可以先进行必要推理，但最后一行必须只写“最终答案：XYZ”，"
            "其中 XYZ 是按字母顺序排列的全部正确选项字母，不要写选项内容。"
        )
    else:
        instruction = (
            "可以先进行必要推理，但最后一行必须只写“最终答案：X”，"
            "其中 X 是正确选项字母；若为多选题则输出全部选项字母，不要写选项内容。"
        )
    if options_text:
        return f"{question}\n{options_text}\n{instruction}"
    return f"{question}\n{instruction}"


def doc_to_target(doc):
    answer = _clean(
        _first(doc, ["answer", "Answer", "raw_answer", "label", "target", "gold", "answer_idx", "answer_index"])
    )
    answer = re.sub(r"^\s*(?:最终答案|答案)\s*[:：]\s*", "", answer)
    options = _options_dict(doc)

    if re.fullmatch(r"\d+", answer):
        idx = int(answer)
        if 0 <= idx < len(LABELS):
            return LABELS[idx]
        if 1 <= idx <= len(LABELS):
            return LABELS[idx - 1]

    normalized = _normalize_choice_letters(answer)
    if normalized:
        return normalized

    for label, text in options.items():
        if answer == text:
            return label
    return answer.upper()


def doc_to_choice(doc):
    _, choices = format_options(doc)
    return choices or ["A", "B", "C", "D", "E"]
