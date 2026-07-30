#!/usr/bin/env python3
"""Build a DAPO-lite training set from prompts with non-zero reward variance.

The goal is to remove GRPO groups that provide no useful policy-gradient signal:
all completions equally bad, equally good, or identical after reward scoring.
The script expects a rollout JSONL with multiple completions per prompt id and a
source RL JSONL containing the original prompt records.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Bad JSON at {path}:{line_no}: {exc}") from exc
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def resolve_path(path: str) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return PROJECT_ROOT / p


def reward_value(row: dict[str, Any], reward_field: str) -> float | None:
    value = row.get(reward_field)
    if value is None and reward_field in {"total_reward", "accuracy_v2", "format_v2"}:
        rewards = row.get("rewards")
        if isinstance(rewards, dict):
            value = rewards.get(reward_field)
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def group_rollouts(rows: list[dict[str, Any]], reward_field: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    match_types: dict[str, Counter[str]] = defaultdict(Counter)

    for row in rows:
        prompt_id = str(row.get("id") or row.get("question_id") or "").strip()
        if not prompt_id:
            continue
        reward = reward_value(row, reward_field)
        if reward is None:
            continue
        grouped[prompt_id].append(reward)
        match_types[prompt_id][str(row.get("match_type") or "unknown")] += 1

    stats: dict[str, dict[str, Any]] = {}
    for prompt_id, rewards in grouped.items():
        unique_rewards = sorted(set(rewards))
        std = statistics.pstdev(rewards) if len(rewards) > 1 else 0.0
        stats[prompt_id] = {
            "count": len(rewards),
            "min": min(rewards),
            "max": max(rewards),
            "mean": sum(rewards) / len(rewards),
            "std": std,
            "unique_rewards": len(unique_rewards),
            "match_types": dict(match_types[prompt_id]),
        }
    return stats


def is_effective_group(
    stats: dict[str, Any],
    *,
    min_group_size: int,
    min_std: float,
    min_reward: float | None,
    max_reward: float | None,
) -> bool:
    if stats["count"] < min_group_size:
        return False
    if stats["unique_rewards"] < 2:
        return False
    if stats["std"] < min_std:
        return False
    if min_reward is not None and stats["max"] < min_reward:
        return False
    if max_reward is not None and stats["min"] > max_reward:
        return False
    return True


def sample_rows(rows: list[dict[str, Any]], max_rows: int, seed: int) -> list[dict[str, Any]]:
    if max_rows <= 0 or len(rows) <= max_rows:
        return rows
    rng = random.Random(seed)
    sampled = list(rows)
    rng.shuffle(sampled)
    return sampled[:max_rows]


def write_report(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# DAPO-lite Effective Dataset Report",
        "",
        f"- Source train file: `{report['train_file']}`",
        f"- Rollout file: `{report['rollout_file']}`",
        f"- Reward field: `{report['reward_field']}`",
        f"- Rollout groups: {report['rollout_groups']}",
        f"- Source train rows: {report['source_train_rows']}",
        f"- Effective ids: {report['effective_ids']}",
        f"- Written train rows: {report['written_train_rows']}",
        f"- Missing ids in source train: {report['missing_ids_in_source_train']}",
        f"- min_group_size: {report['min_group_size']}",
        f"- min_std: {report['min_std']}",
        f"- min_reward: {report['min_reward']}",
        f"- max_reward: {report['max_reward']}",
        "",
        "## Source Distribution",
        "",
    ]
    for key, value in report["source_distribution"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Reward Std Buckets", ""])
    for key, value in report["std_buckets"].items():
        lines.append(f"- {key}: {value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build DAPO-lite effective-gradient dataset.")
    parser.add_argument("--train_file", default="data/final/rl_v3/rl_clean_train.jsonl")
    parser.add_argument("--val_file", default="data/final/rl_v3/rl_clean_val.jsonl")
    parser.add_argument("--rollout_file", default="reports/dapo/sft_v2_a_full_rollout_for_dapo.jsonl")
    parser.add_argument("--output_train", default="data/final/dapo_lite/dapo_effective_train.jsonl")
    parser.add_argument("--output_val", default="data/final/dapo_lite/dapo_effective_val.jsonl")
    parser.add_argument("--report_md", default="reports/dapo/dapo_lite_effective_dataset_report.md")
    parser.add_argument("--report_json", default="reports/dapo/dapo_lite_effective_dataset_report.json")
    parser.add_argument("--reward_field", default="v3_reward")
    parser.add_argument("--min_group_size", type=int, default=2)
    parser.add_argument("--min_std", type=float, default=1e-6)
    parser.add_argument("--min_reward", type=float, default=None)
    parser.add_argument("--max_reward", type=float, default=None)
    parser.add_argument("--max_rows", type=int, default=4000)
    parser.add_argument("--seed", type=int, default=20260626)
    parser.add_argument("--copy_val", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    train_file = resolve_path(args.train_file)
    val_file = resolve_path(args.val_file)
    rollout_file = resolve_path(args.rollout_file)
    output_train = resolve_path(args.output_train)
    output_val = resolve_path(args.output_val)
    report_md = resolve_path(args.report_md)
    report_json = resolve_path(args.report_json)

    train_rows = read_jsonl(train_file)
    rollout_rows = read_jsonl(rollout_file)
    train_by_id = {str(row.get("id")): row for row in train_rows}

    rollout_stats = group_rollouts(rollout_rows, args.reward_field)
    effective_ids = [
        prompt_id
        for prompt_id, stats in rollout_stats.items()
        if is_effective_group(
            stats,
            min_group_size=args.min_group_size,
            min_std=args.min_std,
            min_reward=args.min_reward,
            max_reward=args.max_reward,
        )
    ]
    effective_ids = sorted(effective_ids, key=lambda pid: rollout_stats[pid]["std"], reverse=True)

    missing_ids = [prompt_id for prompt_id in effective_ids if prompt_id not in train_by_id]
    effective_rows = [train_by_id[prompt_id] for prompt_id in effective_ids if prompt_id in train_by_id]
    effective_rows = sample_rows(effective_rows, args.max_rows, args.seed)

    write_jsonl(output_train, effective_rows)
    if args.copy_val:
        write_jsonl(output_val, read_jsonl(val_file))

    std_values = [stats["std"] for stats in rollout_stats.values()]
    std_buckets = {
        "zero": sum(v == 0 for v in std_values),
        "(0,0.05)": sum(0 < v < 0.05 for v in std_values),
        "[0.05,0.2)": sum(0.05 <= v < 0.2 for v in std_values),
        ">=0.2": sum(v >= 0.2 for v in std_values),
    }
    source_distribution = Counter(str(row.get("source") or "unknown") for row in effective_rows)
    report = {
        "train_file": str(train_file),
        "val_file": str(val_file),
        "rollout_file": str(rollout_file),
        "reward_field": args.reward_field,
        "rollout_groups": len(rollout_stats),
        "source_train_rows": len(train_rows),
        "effective_ids": len(effective_ids),
        "written_train_rows": len(effective_rows),
        "missing_ids_in_source_train": len(missing_ids),
        "min_group_size": args.min_group_size,
        "min_std": args.min_std,
        "min_reward": args.min_reward,
        "max_reward": args.max_reward,
        "source_distribution": dict(source_distribution),
        "std_buckets": std_buckets,
        "top_effective_examples": [
            {"id": prompt_id, **rollout_stats[prompt_id]}
            for prompt_id in effective_ids[:20]
        ],
    }
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(report_md, report)

    print(f"Rollout groups: {len(rollout_stats)}")
    print(f"Effective ids: {len(effective_ids)}")
    print(f"Written train rows: {len(effective_rows)} -> {output_train}")
    print(f"Report: {report_md}")
    if len(effective_rows) < 1000:
        print("Warning: effective dataset is small; use a full-train rollout file before final DAPO-lite training.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
