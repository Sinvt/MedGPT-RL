"""
MedGPT-o1 原始训练数据下载与规范化脚本。

默认通过 Hugging Face 镜像站下载数据，只将训练集写入 data/raw/train/。
每个候选数据源都必须完成“加载 -> 规范化 -> JSONL 写入 -> 质量验证”，
验证通过后才会替换成正式文件，避免坏数据污染后续 API 处理流程。

使用示例：
    python scripts/download_raw_data.py
    python scripts/download_raw_data.py --datasets cmb cmexam
    python scripts/download_raw_data.py --force
    python scripts/download_raw_data.py --max-rows 200
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from datasets import get_dataset_config_names, load_dataset
from tqdm import tqdm


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_TRAIN_DIR = PROJECT_ROOT / "data" / "raw" / "train"
DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"
OPTION_LABELS = "ABCDEFGHIJ"


DATASET_SPECS: dict[str, dict[str, Any]] = {
    "cmb": {
        "output": "cmb_train.jsonl",
        "repos": [
            {"path": "FreedomIntelligence/CMB", "name": "exam", "split": "train"},
            {"path": "FreedomIntelligence/CMB", "name": None, "split": "train"},
        ],
        "min_records": 100,
        "min_chinese_ratio": 0.30,
        "require_options": True,
    },
    "cmexam": {
        "output": "cmexam_train.jsonl",
        "repos": [
            {"path": "fzkuji/CMExam", "name": None, "split": "train"},
            {"path": "FreedomIntelligence/CMExam", "name": None, "split": "train"},
        ],
        "min_records": 100,
        "min_chinese_ratio": 0.30,
        "require_options": True,
    },
    "medqa_zh": {
        "output": "medqa_zh_train.jsonl",
        "repos": [
            {"path": "bigbio/med_qa", "name": "med_qa_zh_4options_bigbio_qa", "split": "train"},
            {"path": "bigbio/med_qa", "name": "med_qa_zh_bigbio_qa", "split": "train"},
            {"path": "shibing624/medical", "name": "medqa", "split": "train"},
        ],
        "min_records": 100,
        "min_chinese_ratio": 0.30,
        "require_options": False,
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="通过 Hugging Face 镜像站下载并规范化原始训练数据。",
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help="显示帮助信息并退出。")
    parser.add_argument(
        "--datasets",
        nargs="+",
        choices=sorted(DATASET_SPECS),
        default=["cmb", "cmexam", "medqa_zh"],
        help="需要下载的数据集名称。",
    )
    parser.add_argument(
        "--hf-endpoint",
        default=os.getenv("HF_ENDPOINT", DEFAULT_HF_ENDPOINT),
        help="Hugging Face 访问端点或镜像地址。",
    )
    parser.add_argument("--force", action="store_true", help="即使已有文件验证通过，也强制重新下载并重建。")
    parser.add_argument("--max-rows", type=int, default=0, help="调试用行数上限；0 表示不限制。")
    return parser.parse_args()


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(normalize_text(v) for v in value if v is not None).strip()
    return str(value).strip()


def chinese_ratio(text: str) -> float:
    non_space = sum(not ch.isspace() for ch in text)
    chinese = sum("\u4e00" <= ch <= "\u9fff" for ch in text)
    return chinese / max(non_space, 1)


def first_present(item: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return ""


def normalize_options(options: Any) -> dict[str, str]:
    """将多种选项格式统一为 {"A": "...", "B": "..."}。"""
    if not options:
        return {}

    if isinstance(options, dict):
        normalized: dict[str, str] = {}
        for key, value in options.items():
            label = normalize_text(key).upper()
            text = normalize_text(value)
            if label and text:
                normalized[label[0]] = text
        return normalized

    if isinstance(options, list):
        normalized = {}
        for i, item in enumerate(options):
            if i >= len(OPTION_LABELS):
                break
            if isinstance(item, dict):
                label = normalize_text(
                    item.get("key")
                    or item.get("label")
                    or item.get("name")
                    or item.get("option")
                    or OPTION_LABELS[i]
                ).upper()
                text = normalize_text(
                    item.get("value")
                    or item.get("text")
                    or item.get("content")
                    or item.get("answer")
                    or item.get("statement")
                )
            else:
                label = OPTION_LABELS[i]
                text = normalize_text(item)
            if label and text:
                normalized[label[0]] = text
        return normalized

    if isinstance(options, str):
        text = options.strip()
        if not text or text in {"[]", "{}"}:
            return {}
        pattern = r"(?:^|\s)([A-J])[\.\．、:：]\s*(.*?)(?=\s+[A-J][\.\．、:：]\s*|$)"
        matches = re.findall(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if matches:
            return {label.upper(): value.strip() for label, value in matches if value.strip()}
        return {"RAW": text}

    return {}


def options_to_text(options: dict[str, str]) -> str:
    if not options:
        return ""
    if "RAW" in options:
        return options["RAW"]
    return "  ".join(f"{label}. {text}" for label, text in sorted(options.items()))


def answer_to_text(answer: Any, options: dict[str, str]) -> tuple[str, str]:
    raw = normalize_text(answer)
    if not raw:
        return "", ""

    key = raw.upper().replace("答案：", "").replace("答案:", "").strip()
    looks_like_letters = re.fullmatch(r"[\sA-J,，、]+", key) is not None
    if looks_like_letters:
        key = re.sub(r"[^A-J]", "", key)
        if len(key) == 1 and key in options:
            return key, options[key]
        # 只有字母答案但没有选项文本时，不能作为可验证短答案使用。
        return key, ""

    for label, text in options.items():
        if raw == text or raw.lower() == text.lower():
            return label, text

    return raw, raw


def normalize_record(dataset_name: str, idx: int, item: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any] | None:
    question = normalize_text(
        first_present(
            item,
            [
                "question",
                "Question",
                "exam_question",
                "ExamQuestion",
                "query",
                "prompt",
                "problem",
                "body",
                "stem",
            ],
        )
    )

    options_value = first_present(
        item,
        [
            "options",
            "Options",
            "option",
            "Option",
            "option_str",
            "choices",
            "Choices",
            "choice",
            "answer_choices",
        ],
    )

    explicit_options: dict[str, str] = {}
    for label in OPTION_LABELS:
        value = first_present(
            item,
            [
                f"option_{label}",
                f"Option_{label}",
                f"option{label}",
                f"Option{label}",
                f"choice_{label}",
                f"Choice_{label}",
                label,
            ],
        )
        if value:
            explicit_options[label] = normalize_text(value)

    options = explicit_options or normalize_options(options_value)
    if spec["require_options"] and not options:
        return None

    answer = first_present(
        item,
        [
            "answer",
            "Answer",
            "answers",
            "Answers",
            "answer_idx",
            "answer_index",
            "label",
            "target",
            "gold",
        ],
    )
    answer_key, answer_text = answer_to_text(answer, options)

    if not question or not answer_text:
        return None

    return {
        "id": f"{dataset_name}_{idx:06d}",
        "source": dataset_name,
        "split": "train",
        "question": question,
        "options": options_to_text(options),
        "answer": answer_key,
        "answer_text": answer_text,
        "raw_answer": normalize_text(answer),
    }


def candidate_configs(dataset_name: str) -> list[dict[str, Any]]:
    """返回严格的 train split 候选源；训练数据绝不回退到 val/test。"""
    spec = DATASET_SPECS[dataset_name]
    candidates = list(spec["repos"])

    for repo in spec["repos"]:
        path = repo["path"]
        try:
            configs = get_dataset_config_names(path, trust_remote_code=True)
        except Exception:
            continue
        for config in configs:
            if dataset_name == "medqa_zh" and "zh" not in config.lower() and "medqa" not in config.lower():
                continue
            item = {"path": path, "name": config, "split": "train"}
            if item not in candidates:
                candidates.append(item)
    return candidates


def load_candidate(candidate: dict[str, Any]) -> Any:
    path = candidate["path"]
    name = candidate["name"]
    split = candidate["split"]
    print(f"  尝试加载 {path} / {name or '<默认配置>'} / {split}")
    if name:
        return load_dataset(path, name, split=split, trust_remote_code=True)
    return load_dataset(path, split=split, trust_remote_code=True)


def validation_min_records(spec: dict[str, Any], max_rows: int) -> int:
    if max_rows:
        return min(spec["min_records"], max_rows)
    return spec["min_records"]


def validate_jsonl(path: Path, spec: dict[str, Any], max_rows: int = 0) -> tuple[bool, dict[str, Any]]:
    stats = {
        "rows": 0,
        "bad_json": 0,
        "missing_question": 0,
        "missing_answer": 0,
        "missing_options": 0,
        "chinese_like": 0,
    }

    if not path.exists() or path.stat().st_size == 0:
        return False, stats

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            stats["rows"] += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                stats["bad_json"] += 1
                continue
            question = normalize_text(obj.get("question"))
            answer_text = normalize_text(obj.get("answer_text") or obj.get("answer"))
            options = normalize_text(obj.get("options"))
            if not question:
                stats["missing_question"] += 1
            if not answer_text:
                stats["missing_answer"] += 1
            if not options:
                stats["missing_options"] += 1
            if chinese_ratio(question) >= spec["min_chinese_ratio"]:
                stats["chinese_like"] += 1

    rows = stats["rows"]
    if rows < validation_min_records(spec, max_rows):
        return False, stats
    if stats["bad_json"] > 0:
        return False, stats
    if stats["missing_question"] or stats["missing_answer"]:
        return False, stats
    if spec["require_options"] and stats["missing_options"] / rows > 0.05:
        return False, stats
    if stats["chinese_like"] / rows < 0.60:
        return False, stats
    return True, stats


def write_candidate(dataset_name: str, ds: Any, tmp_path: Path, max_rows: int) -> tuple[int, int]:
    spec = DATASET_SPECS[dataset_name]
    count = 0
    skipped = 0
    with tmp_path.open("w", encoding="utf-8") as f:
        for idx, item in enumerate(tqdm(ds, desc=f"规范化 {dataset_name}")):
            record = normalize_record(dataset_name, idx, dict(item), spec)
            if record is None:
                skipped += 1
                continue
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
            if max_rows and count >= max_rows:
                break
    return count, skipped


def write_dataset(dataset_name: str, force: bool, max_rows: int) -> bool:
    spec = DATASET_SPECS[dataset_name]
    output_path = RAW_TRAIN_DIR / spec["output"]
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")

    if not force:
        ok, stats = validate_jsonl(output_path, spec, max_rows=max_rows)
        if ok:
            print(f"[通过] {dataset_name}: 现有文件验证合格：{output_path}")
            print(f"       统计信息：{stats}")
            return True
        if output_path.exists():
            print(f"[警告] {dataset_name}: 现有文件验证失败，将重新构建。")
            print(f"       统计信息：{stats}")

    RAW_TRAIN_DIR.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []

    for candidate in candidate_configs(dataset_name):
        try:
            ds = load_candidate(candidate)
            count, skipped = write_candidate(dataset_name, ds, tmp_path, max_rows)
            ok, stats = validate_jsonl(tmp_path, spec, max_rows=max_rows)
            print(
                f"  候选源写入 {count} 行，跳过 {skipped} 行；"
                f"验证结果={ok}，统计信息={stats}"
            )
            if ok:
                tmp_path.replace(output_path)
                print(f"[完成] {dataset_name}: {output_path}")
                return True
            tmp_path.unlink(missing_ok=True)
            errors.append(f"{candidate}: 验证失败 {stats}")
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            errors.append(f"{candidate}: {exc}")

    print(f"[错误] {dataset_name}: 没有任何候选数据源通过验证。")
    for err in errors[-8:]:
        print(f"  - {err}")
    return False


def warn_legacy_raw_files() -> None:
    legacy_files = sorted((PROJECT_ROOT / "data" / "raw").glob("*.jsonl"))
    if not legacy_files:
        return
    print("[警告] 发现位于 data/raw/train/ 之外的旧 raw 文件，后续流程不会使用它们：")
    for path in legacy_files:
        print(f"  - {path}")


def main() -> int:
    args = parse_args()
    os.environ["HF_ENDPOINT"] = args.hf_endpoint
    RAW_TRAIN_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("MedGPT-o1 原始训练数据下载器")
    print(f"项目根目录：{PROJECT_ROOT}")
    print(f"输出目录  ：{RAW_TRAIN_DIR}")
    print(f"HF 镜像   ：{os.environ['HF_ENDPOINT']}")
    print(f"数据集    ：{', '.join(args.datasets)}")
    print("=" * 72)
    warn_legacy_raw_files()

    results = {}
    for dataset_name in args.datasets:
        print("\n" + "-" * 72)
        print(f"处理数据集：{dataset_name}")
        print("-" * 72)
        results[dataset_name] = write_dataset(dataset_name, force=args.force, max_rows=args.max_rows)

    print("\n" + "=" * 72)
    print("下载结果汇总")
    print("=" * 72)
    for dataset_name, ok in results.items():
        print(f"{dataset_name:10s} {'通过' if ok else '失败'}")

    if not all(results.values()):
        print("\n存在未通过验证的数据集。修复 raw 数据之前，请勿进入 Phase 1。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
