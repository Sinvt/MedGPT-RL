import argparse
import json
import logging
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
IN_FILE = PROJECT_ROOT / "data" / "intermediate" / "cot_filtered.jsonl"
FINAL_DIR = PROJECT_ROOT / "data" / "final" / "sft"
REPORT_DIR = PROJECT_ROOT / "reports"

DEFAULT_SYSTEM_PROMPT = "你是一个严谨的中文医学推理助手。请基于题干进行医学推理，并给出最终答案。"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def get_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"找不到输入文件：{path}")

    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                logger.warning(f"第 {line_no} 行 JSON 解析失败，已跳过：{exc}")
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def get_group_id(item: dict) -> str:
    return (
        get_text(item.get("openqa_id"))
        or get_text(item.get("mcmle_id"))
        or get_text(item.get("source_id"))
        or get_text(item.get("id"))
    )


def select_top_per_question(rows: list[dict], top_k: int) -> list[dict]:
    group_to_rows = defaultdict(list)
    for row in rows:
        group_to_rows[get_group_id(row)].append(row)

    selected = []
    for _, group_rows in group_to_rows.items():
        group_rows.sort(
            key=lambda row: (
                float(row.get("quality_score", 0.0)),
                float(row.get("orm_score", 0.0)),
                int(row.get("quality_step_count", 0)),
                -int(row.get("path_id", 9999)),
            ),
            reverse=True,
        )
        selected.extend(group_rows[:top_k])
    return selected


def split_by_question(rows: list[dict], val_ratio: float, seed: int) -> tuple[list[dict], list[dict]]:
    group_to_rows = defaultdict(list)
    for row in rows:
        group_to_rows[get_group_id(row)].append(row)

    groups = list(group_to_rows)
    rng = random.Random(seed)
    rng.shuffle(groups)

    val_count = max(1, int(len(groups) * val_ratio)) if groups else 0
    val_groups = set(groups[:val_count])

    train = []
    val = []
    for group_id, group_rows in group_to_rows.items():
        if group_id in val_groups:
            val.extend(group_rows)
        else:
            train.extend(group_rows)
    return train, val


def build_sft_record(item: dict, system_prompt: str) -> dict:
    question = get_text(item.get("question"))
    cot_content = get_text(item.get("cot_content"))
    if not question:
        raise ValueError(f"样本缺少 question：{item.get('id')}")
    if not cot_content:
        raise ValueError(f"样本缺少 cot_content：{item.get('id')}")

    return {
        "id": f"sft_{get_group_id(item)}",
        "source_cot_id": get_text(item.get("id")),
        "openqa_id": get_text(item.get("openqa_id")),
        "source_id": get_text(item.get("source_id")),
        "source": get_text(item.get("source")),
        "split": get_text(item.get("split")) or "train",
        "question": question,
        "standard_answer": get_text(item.get("standard_answer")),
        "answer_aliases": item.get("answer_aliases") if isinstance(item.get("answer_aliases"), list) else [],
        "quality_score": float(item.get("quality_score", 0.0)),
        "quality_step_count": int(item.get("quality_step_count", 0)),
        "quality_think_chars": int(item.get("quality_think_chars", 0)),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
            {"role": "assistant", "content": cot_content},
        ],
    }


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {
            "rows": 0,
            "questions": 0,
            "sources": {},
            "avg_steps": 0.0,
            "avg_think_chars": 0.0,
        }

    steps = [int(row.get("quality_step_count", 0)) for row in rows]
    think_chars = [int(row.get("quality_think_chars", 0)) for row in rows]
    return {
        "rows": len(rows),
        "questions": len({get_group_id(row) for row in rows}),
        "sources": dict(Counter(row.get("source") for row in rows)),
        "avg_steps": statistics.mean(steps) if steps else 0.0,
        "avg_think_chars": statistics.mean(think_chars) if think_chars else 0.0,
    }


def format_counter(counter: dict) -> str:
    if not counter:
        return "无"
    return "，".join(f"{key}: {value}" for key, value in sorted(counter.items(), key=lambda x: str(x[0])))


def write_report(args, input_rows: list[dict], selected_rows: list[dict], train_rows: list[dict], val_rows: list[dict]) -> None:
    input_summary = summarize(input_rows)
    selected_summary = summarize(selected_rows)
    train_summary = summarize(train_rows)
    val_summary = summarize(val_rows)

    report = f"""# SFT Dataset Report

## 输入输出

- 输入文件：`{args.input}`
- 训练集：`{args.output_train}`
- 验证集：`{args.output_val}`
- 全量选中样本：`{args.output_all}`
- 每题保留 CoT 数：{args.top_k}
- 验证集比例：{args.val_ratio}

## 输入数据

- CoT 总数：{input_summary['rows']}
- 覆盖题目数：{input_summary['questions']}
- 来源分布：{format_counter(input_summary['sources'])}

## 选中数据

- SFT 样本数：{selected_summary['rows']}
- 覆盖题目数：{selected_summary['questions']}
- 来源分布：{format_counter(selected_summary['sources'])}
- 平均推理步骤数：{selected_summary['avg_steps']:.2f}
- 平均 `<think>` 内容长度：{selected_summary['avg_think_chars']:.2f}

## 训练/验证切分

- train 样本数：{train_summary['rows']}
- train 来源分布：{format_counter(train_summary['sources'])}
- val 样本数：{val_summary['rows']}
- val 来源分布：{format_counter(val_summary['sources'])}
- 切分方式：按题目分组切分，避免同一道题同时出现在 train 和 val。

## 数据格式

每条样本包含 `messages` 字段，可直接用于 ChatML/ShareGPT 风格 SFT：

```json
{{
  "messages": [
    {{"role": "system", "content": "..."}},
    {{"role": "user", "content": "开放式医学问题"}},
    {{"role": "assistant", "content": "<think>...</think>\\n最终答案：..."}}
  ]
}}
```
"""
    args.report.write_text(report, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="构造 SFT 数据集")
    parser.add_argument("--input", type=Path, default=IN_FILE, help="过滤后的 CoT 输入文件")
    parser.add_argument("--output_train", type=Path, default=FINAL_DIR / "sft_train.jsonl", help="SFT 训练集输出文件")
    parser.add_argument("--output_val", type=Path, default=FINAL_DIR / "sft_val.jsonl", help="SFT 验证集输出文件")
    parser.add_argument("--output_all", type=Path, default=FINAL_DIR / "sft_all.jsonl", help="SFT 全量选中样本输出文件")
    parser.add_argument("--report", type=Path, default=REPORT_DIR / "sft_dataset_report.md", help="SFT 数据报告")
    parser.add_argument("--top_k", type=int, default=1, help="每道题保留多少条 CoT；主线建议 1")
    parser.add_argument("--val_ratio", type=float, default=0.1, help="验证集题目比例")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--system_prompt", type=str, default=DEFAULT_SYSTEM_PROMPT, help="SFT system prompt")
    args = parser.parse_args()

    if args.top_k <= 0:
        raise ValueError("--top_k 必须大于 0")
    if not 0 < args.val_ratio < 1:
        raise ValueError("--val_ratio 必须在 0 和 1 之间")

    args.output_train.parent.mkdir(parents=True, exist_ok=True)
    args.output_val.parent.mkdir(parents=True, exist_ok=True)
    args.output_all.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)

    input_rows = load_jsonl(args.input)
    selected_rows = select_top_per_question(input_rows, args.top_k)
    sft_rows = [build_sft_record(row, args.system_prompt) for row in selected_rows]
    train_rows, val_rows = split_by_question(sft_rows, args.val_ratio, args.seed)

    rng = random.Random(args.seed)
    rng.shuffle(train_rows)
    rng.shuffle(val_rows)
    rng.shuffle(sft_rows)

    write_jsonl(args.output_all, sft_rows)
    write_jsonl(args.output_train, train_rows)
    write_jsonl(args.output_val, val_rows)
    write_report(args, input_rows, sft_rows, train_rows, val_rows)

    selected_summary = summarize(sft_rows)
    train_summary = summarize(train_rows)
    val_summary = summarize(val_rows)

    logger.info("=" * 50)
    logger.info(f"输入 CoT：{len(input_rows)} 条。")
    logger.info(f"选中 SFT 样本：{selected_summary['rows']} 条，覆盖题目 {selected_summary['questions']} 道。")
    logger.info(f"训练集：{train_summary['rows']} 条；验证集：{val_summary['rows']} 条。")
    logger.info(f"训练集保存至：{args.output_train}")
    logger.info(f"验证集保存至：{args.output_val}")
    logger.info(f"全量选中样本保存至：{args.output_all}")
    logger.info(f"报告保存至：{args.report}")
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
