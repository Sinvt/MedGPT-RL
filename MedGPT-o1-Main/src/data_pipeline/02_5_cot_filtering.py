import argparse
import json
import logging
import re
import sys
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
IN_FILE = PROJECT_ROOT / "data" / "intermediate" / "cot_candidates.jsonl"
OUT_DIR = PROJECT_ROOT / "data" / "intermediate"
REPORT_DIR = PROJECT_ROOT / "reports"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from rewards.orm_reward import score_response  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


LEAKAGE_PATTERNS = [
    r"标准答案",
    r"给定答案",
    r"已知答案",
    r"根据答案",
    r"答案已[经]?给出",
    r"题目要求.*答案",
    r"作为.*标注",
]

OPTION_LEAK_PATTERNS = [
    r"(?:^|[\n\r])\s*[A-H][\.．、:：]\s*",
    r"选项[A-H]",
    r"正确选项",
    r"错误选项",
    r"下列哪[一]?项",
    r"以下哪[一]?项",
]

PLACEHOLDER_PATTERNS = [
    r"无法判断",
    r"信息不足",
    r"不确定",
    r"需要更多信息",
    r"不能确定",
]


@dataclass
class QualityResult:
    passed: bool
    score: float
    reasons: list[str]
    step_count: int
    think_chars: int


def get_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def normalize_text(text: str) -> str:
    text = get_text(text).lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，,。.;；:：、!?！？（）()\[\]【】\"'“”‘’]", "", text)
    return text


def extract_think(cot_content: str) -> str:
    match = re.search(r"<think>(.*?)</think>", cot_content or "", flags=re.DOTALL | re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).strip()


def extract_steps(think_text: str) -> list[str]:
    lines = [line.strip() for line in think_text.splitlines() if line.strip()]
    steps = []
    for line in lines:
        line = re.sub(r"^\s*(?:\d+[\.\)、)]|[-*])\s*", "", line).strip()
        if line:
            steps.append(line)

    if len(steps) <= 1 and think_text:
        parts = re.split(r"(?<=[。！？!?])\s*", think_text)
        steps = [part.strip() for part in parts if part.strip()]
    return steps


def contains_any_pattern(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return pattern
    return None


def duplicate_step_count(steps: list[str]) -> int:
    normalized_steps = [normalize_text(step) for step in steps if normalize_text(step)]
    counts = Counter(normalized_steps)
    return sum(count - 1 for count in counts.values() if count > 1)


def get_group_id(item: dict) -> str:
    for key in ("openqa_id", "mcmle_id", "source_id"):
        value = get_text(item.get(key))
        if value:
            return value
    return normalize_text(get_text(item.get("question")) + get_text(item.get("standard_answer")))


def evaluate_quality(item: dict, args) -> QualityResult:
    reasons = []
    score = 100.0

    cot_content = get_text(item.get("cot_content"))
    final_answer = get_text(item.get("final_answer"))
    think_text = extract_think(cot_content)
    steps = extract_steps(think_text)
    step_count = len(steps)
    think_chars = len(think_text)

    if not get_text(item.get("id")):
        reasons.append("缺少 id")
        score -= 30
    if not get_text(item.get("question")):
        reasons.append("缺少 question")
        score -= 30
    if not get_text(item.get("standard_answer")):
        reasons.append("缺少 standard_answer")
        score -= 30

    answer_aliases = item.get("answer_aliases")
    if not isinstance(answer_aliases, list):
        answer_aliases = []
    orm_result = score_response(cot_content, get_text(item.get("standard_answer")), answer_aliases)
    item["orm_score"] = orm_result.score
    item["orm_matched"] = orm_result.matched
    item["orm_match_type"] = orm_result.match_type
    item["orm_predicted_answer"] = orm_result.predicted_answer
    item["orm_matched_answer"] = orm_result.matched_answer

    if args.require_orm and not orm_result.matched:
        reasons.append("ORM 未命中标准答案")
        score -= 50

    if "<think>" not in cot_content or "</think>" not in cot_content:
        reasons.append("缺少 think 结构")
        score -= 40
    if "最终答案" not in cot_content:
        reasons.append("缺少最终答案标记")
        score -= 30
    if not final_answer:
        reasons.append("final_answer 为空")
        score -= 30

    if step_count < args.min_steps:
        reasons.append(f"推理步骤过少：{step_count}")
        score -= 20
    if step_count > args.max_steps:
        reasons.append(f"推理步骤过多：{step_count}")
        score -= 8

    if think_chars < args.min_think_chars:
        reasons.append(f"推理内容过短：{think_chars} 字")
        score -= 20
    if think_chars > args.max_think_chars:
        reasons.append(f"推理内容过长：{think_chars} 字")
        score -= 8

    has_hard_leakage = False
    has_hard_option_leak = False

    leakage = contains_any_pattern(cot_content, LEAKAGE_PATTERNS)
    if leakage:
        reasons.append(f"疑似标注泄漏：{leakage}")
        has_hard_leakage = True
        score -= 35

    option_leak = contains_any_pattern(cot_content, OPTION_LEAK_PATTERNS)
    if option_leak:
        reasons.append(f"疑似选择题痕迹：{option_leak}")
        has_hard_option_leak = True
        score -= 20

    placeholder = contains_any_pattern(cot_content, PLACEHOLDER_PATTERNS)
    if placeholder:
        reasons.append(f"疑似空泛拒答：{placeholder}")
        score -= 20

    repeated_steps = duplicate_step_count(steps)
    if repeated_steps:
        reasons.append(f"重复推理步骤：{repeated_steps}")
        score -= min(20, repeated_steps * 8)

    if 4 <= step_count <= 6:
        score += 5
    if args.min_think_chars <= think_chars <= min(args.max_think_chars, 700):
        score += 5

    hard_fail_reasons = [
        "缺少 id",
        "缺少 question",
        "缺少 standard_answer",
        "ORM 未命中标准答案",
        "缺少 think 结构",
        "缺少最终答案标记",
        "final_answer 为空",
    ]
    if args.hard_reject_leakage and has_hard_leakage:
        hard_fail_reasons.append(f"疑似标注泄漏：{leakage}")
    if args.hard_reject_option_leak and has_hard_option_leak:
        hard_fail_reasons.append(f"疑似选择题痕迹：{option_leak}")

    passed = score >= args.min_score and not any(reason in reasons for reason in hard_fail_reasons)
    return QualityResult(
        passed=passed,
        score=max(score, 0.0),
        reasons=reasons,
        step_count=step_count,
        think_chars=think_chars,
    )


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


def summarize_numbers(values: list[int]) -> str:
    if not values:
        return "无"
    return (
        f"平均 {statistics.mean(values):.2f}，"
        f"中位数 {statistics.median(values):.2f}，"
        f"最小 {min(values)}，最大 {max(values)}"
    )


def deduplicate_rows(rows: list[dict], duplicate_policy: str) -> tuple[list[dict], list[dict]]:
    """按候选 id 去重；默认保留最后一次出现的记录，方便后续重跑修正样本。"""
    if duplicate_policy == "keep_first":
        seen_ids = set()
        unique_rows = []
        duplicate_rows = []
        for row in rows:
            row_id = get_text(row.get("id"))
            if row_id and row_id in seen_ids:
                duplicate_rows.append(row)
                continue
            if row_id:
                seen_ids.add(row_id)
            unique_rows.append(row)
        return unique_rows, duplicate_rows

    if duplicate_policy == "keep_last":
        latest_by_id = {}
        anonymous_rows = []
        duplicate_rows = []
        for row in rows:
            row_id = get_text(row.get("id"))
            if not row_id:
                anonymous_rows.append(row)
                continue
            if row_id in latest_by_id:
                duplicate_rows.append(latest_by_id[row_id])
            latest_by_id[row_id] = row
        return anonymous_rows + list(latest_by_id.values()), duplicate_rows

    raise ValueError(f"未知 duplicate_policy：{duplicate_policy}")


def build_report(
    args,
    total_rows: int,
    unique_rows: int,
    accepted: list[dict],
    rejected: list[dict],
    all_reason_counter: Counter,
    source_counter: Counter,
    accepted_source_counter: Counter,
    groups_before: int,
    groups_after: int,
) -> str:
    accepted_step_counts = [int(row.get("quality_step_count", 0)) for row in accepted]
    accepted_think_chars = [int(row.get("quality_think_chars", 0)) for row in accepted]

    top_reasons = "\n".join(
        f"- {reason}：{count} 条" for reason, count in all_reason_counter.most_common(15)
    ) or "- 无"

    source_lines = "\n".join(
        f"- {source}：原始 {source_counter[source]} 条，保留 {accepted_source_counter.get(source, 0)} 条"
        for source in sorted(source_counter)
    ) or "- 无"

    return f"""# CoT 质量过滤报告

## 基本信息

- 输入文件：`{args.input}`
- 输出文件：`{args.output}`
- 拒绝文件：`{args.rejected_output}`
- 每题最多保留：{args.top_k} 条
- 最低质量分：{args.min_score}
- 是否要求 ORM 命中：{args.require_orm}
- 是否硬拒绝标注泄漏：{args.hard_reject_leakage}
- 是否硬拒绝选择题痕迹：{args.hard_reject_option_leak}
- 重复 id 处理策略：{args.duplicate_policy}
- 运行策略：全量重建输出文件，不追加写入

## 总体结果

- 输入候选总数：{total_rows}
- 去重后候选数：{unique_rows}
- 原始覆盖题目数：{groups_before}
- 过滤后候选数：{len(accepted)}
- 过滤后覆盖题目数：{groups_after}
- 拒绝候选数：{len(rejected)}
- 保留率：{len(accepted) / unique_rows * 100 if unique_rows else 0:.2f}%

## 来源分布

{source_lines}

## 保留样本形态

- 推理步骤数：{summarize_numbers(accepted_step_counts)}
- `<think>` 内容长度：{summarize_numbers(accepted_think_chars)}

## 主要拒绝原因

{top_reasons}
"""


def main():
    parser = argparse.ArgumentParser(description="CoT 候选质量过滤脚本（Phase 2.5）")
    parser.add_argument("--input", type=Path, default=IN_FILE, help="CoT 候选输入文件")
    parser.add_argument("--output", type=Path, default=OUT_DIR / "cot_filtered.jsonl", help="过滤后的 CoT 输出文件")
    parser.add_argument("--rejected_output", type=Path, default=OUT_DIR / "cot_rejected.jsonl", help="被拒绝 CoT 输出文件")
    parser.add_argument("--report", type=Path, default=REPORT_DIR / "cot_quality_report.md", help="质量报告输出文件")
    parser.add_argument("--top_k", type=int, default=3, help="每道题最多保留多少条 CoT")
    parser.add_argument("--min_score", type=float, default=70.0, help="最低质量分")
    parser.add_argument("--min_steps", type=int, default=3, help="最少推理步骤数")
    parser.add_argument("--max_steps", type=int, default=8, help="最多推理步骤数")
    parser.add_argument("--min_think_chars", type=int, default=80, help="think 内容最少字符数")
    parser.add_argument("--max_think_chars", type=int, default=1200, help="think 内容最多字符数")
    parser.add_argument("--require_orm", action="store_true", default=True, help="要求 ORM 命中标准答案")
    parser.add_argument("--allow_orm_failed", action="store_false", dest="require_orm", help="允许 ORM 未命中的样本进入候选排序")
    parser.add_argument("--hard_reject_leakage", action="store_true", default=True, help="硬拒绝疑似标注泄漏样本")
    parser.add_argument("--allow_leakage", action="store_false", dest="hard_reject_leakage", help="不硬拒绝疑似标注泄漏样本，只按质量分扣分")
    parser.add_argument("--hard_reject_option_leak", action="store_true", default=True, help="硬拒绝疑似选择题痕迹样本")
    parser.add_argument("--allow_option_leak", action="store_false", dest="hard_reject_option_leak", help="不硬拒绝疑似选择题痕迹样本，只按质量分扣分")
    parser.add_argument("--duplicate_policy", choices=["keep_last", "keep_first"], default="keep_last", help="cot_candidates 中出现重复 id 时保留哪一条；后续重跑修正样本时建议 keep_last")
    args = parser.parse_args()

    if args.top_k <= 0:
        raise ValueError("--top_k 必须大于 0")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.rejected_output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)

    rows = load_jsonl(args.input)
    source_counter = Counter(get_text(row.get("source")) or "unknown" for row in rows)

    unique_rows, duplicate_rows = deduplicate_rows(rows, args.duplicate_policy)
    duplicate_rejections = []
    for row in duplicate_rows:
        duplicate = dict(row)
        duplicate["quality_score"] = 0.0
        duplicate["quality_passed"] = False
        duplicate["reject_reasons"] = [f"重复 id，已按 {args.duplicate_policy} 策略丢弃"]
        duplicate_rejections.append(duplicate)

    group_to_rows = defaultdict(list)
    rejected = list(duplicate_rejections)
    all_reason_counter = Counter(reason for row in duplicate_rejections for reason in row["reject_reasons"])

    for row in unique_rows:
        result = evaluate_quality(row, args)
        enriched = dict(row)
        enriched["quality_score"] = round(result.score, 2)
        enriched["quality_passed"] = result.passed
        enriched["quality_step_count"] = result.step_count
        enriched["quality_think_chars"] = result.think_chars
        enriched["reject_reasons"] = result.reasons

        if result.reasons:
            all_reason_counter.update(result.reasons)

        if result.passed:
            group_to_rows[get_group_id(enriched)].append(enriched)
        else:
            rejected.append(enriched)

    accepted = []
    for _, group_rows in group_to_rows.items():
        group_rows.sort(
            key=lambda row: (
                float(row.get("quality_score", 0.0)),
                int(row.get("quality_step_count", 0)),
                -int(row.get("path_id", 9999)),
            ),
            reverse=True,
        )
        accepted.extend(group_rows[: args.top_k])
        rejected.extend(group_rows[args.top_k :])

    accepted.sort(key=lambda row: (get_group_id(row), int(row.get("path_id", 0))))
    rejected.sort(key=lambda row: get_text(row.get("id")))

    write_jsonl(args.output, accepted)
    write_jsonl(args.rejected_output, rejected)

    accepted_source_counter = Counter(get_text(row.get("source")) or "unknown" for row in accepted)
    groups_before = len({get_group_id(row) for row in unique_rows})
    groups_after = len({get_group_id(row) for row in accepted})
    report = build_report(
        args,
        total_rows=len(rows),
        unique_rows=len(unique_rows),
        accepted=accepted,
        rejected=rejected,
        all_reason_counter=all_reason_counter,
        source_counter=source_counter,
        accepted_source_counter=accepted_source_counter,
        groups_before=groups_before,
        groups_after=groups_after,
    )
    args.report.write_text(report, encoding="utf-8")

    logger.info("=" * 50)
    logger.info(f"输入候选：{len(rows)} 条；去重后：{len(unique_rows)} 条。")
    logger.info(f"过滤后保留：{len(accepted)} 条，覆盖题目：{groups_after}/{groups_before}。")
    logger.info(f"拒绝样本：{len(rejected)} 条。")
    logger.info(f"过滤数据保存至：{args.output}")
    logger.info(f"拒绝数据保存至：{args.rejected_output}")
    logger.info(f"质量报告保存至：{args.report}")
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
