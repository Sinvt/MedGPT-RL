import argparse
from collections import Counter
import json
import random
from pathlib import Path


SYSTEM_PROMPT = (
    "你是一个严谨的中文医学推理助手。请基于题干进行医学推理，"
    "先使用 <think>...</think> 写出必要推理过程，再用 `最终答案：` 给出简洁答案。"
)


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_report(path, args, total_input, records, train_records, val_records, skipped):
    source_counter = Counter(record["source"] for record in records)
    train_source_counter = Counter(record["source"] for record in train_records)
    val_source_counter = Counter(record["source"] for record in val_records)

    report = [
        "# RL Dataset Report",
        "",
        "## 输入输出",
        "",
        f"- 输入文件：`{Path(args.input_file).resolve()}`",
        f"- 输出目录：`{Path(args.output_dir).resolve()}`",
        f"- 训练集：`{(Path(args.output_dir) / 'rl_train.jsonl').resolve()}`",
        f"- 验证集：`{(Path(args.output_dir) / 'rl_val.jsonl').resolve()}`",
        f"- 验证集比例：{args.val_ratio}",
        f"- 随机种子：{args.seed}",
        "",
        "## 数据统计",
        "",
        f"- 原始读取条数：{total_input}",
        f"- 有效 RL 样本数：{len(records)}",
        f"- 跳过样本数：{skipped}",
        f"- 来源分布：{dict(source_counter)}",
        "",
        "## 训练/验证切分",
        "",
        f"- train 样本数：{len(train_records)}",
        f"- train 来源分布：{dict(train_source_counter)}",
        f"- val 样本数：{len(val_records)}",
        f"- val 来源分布：{dict(val_source_counter)}",
        "",
        "## 数据格式",
        "",
        "每条样本包含 `prompt`、`question`、`standard_answer`、`answer_aliases` 等字段。"
        "后续 GRPO/DAPO 只把 `prompt` 喂给模型生成回答，"
        "`standard_answer` 和 `answer_aliases` 用于 ORM 奖励计算。",
        "",
    ]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(report), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="构造 GRPO/RL 强化学习数据集")
    parser.add_argument("--input_file", type=str, default="data/intermediate/openqa_raw.jsonl", help="开放式问答原数据")
    parser.add_argument("--output_dir", type=str, default="data/final/rl", help="输出目录")
    parser.add_argument("--report_file", type=str, default="reports/rl_dataset_report.md", help="数据集报告输出路径")
    parser.add_argument("--val_ratio", type=float, default=0.02, help="验证集比例")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少条样本，0 表示全量")
    parser.add_argument("--system_prompt", type=str, default=SYSTEM_PROMPT, help="RL 生成时使用的 system prompt")
    args = parser.parse_args()

    if not 0 <= args.val_ratio < 1:
        raise ValueError("--val_ratio 必须在 [0, 1) 范围内。")

    input_path = Path(args.input_file)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    train_out = output_dir / "rl_train.jsonl"
    val_out = output_dir / "rl_val.jsonl"
    
    records = []
    skipped = 0
    total_input = 0
    with open(input_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total_input += 1
            item = json.loads(line)
            
            question = item.get("question")
            standard_answer = item.get("standard_answer", "")
            if not question or not standard_answer:
                skipped += 1
                continue
                
            # GRPO 等 RL 框架通常期望输入列叫做 'prompt'。
            # 为了兼容带有 Chat Template 的模型，我们将其构造成 messages 列表格式。
            # 强化学习时，模型只需看到题干，不需要看到 CoT，让它自己探索。
            prompt = [
                {"role": "system", "content": args.system_prompt},
                {"role": "user", "content": question}
            ]
            
            rl_item = {
                "id": item.get("id"),
                "source_id": item.get("source_id"),
                "prompt": prompt,
                "question": question,
                "standard_answer": standard_answer,
                "answer_aliases": item.get("answer_aliases", []),
                "source": item.get("source", "unknown"),
                "original_split": item.get("split"),
                "verifiable": item.get("verifiable", True),
            }
            records.append(rl_item)
            if args.limit and len(records) >= args.limit:
                break
            
    print(f"从 {args.input_file} 读取 {total_input} 条，构造有效 RL 样本 {len(records)} 条，跳过 {skipped} 条。")
    
    # 打乱后切分 Train / Val
    rng = random.Random(args.seed)
    rng.shuffle(records)
    val_size = int(len(records) * args.val_ratio)
    val_records = records[:val_size]
    train_records = records[val_size:]
    
    write_jsonl(train_out, train_records)
    write_jsonl(val_out, val_records)
    build_report(args.report_file, args, total_input, records, train_records, val_records, skipped)
            
    print("\n构建 RL 数据集完成！")
    print(f"[{train_out.name}] : 训练集 {len(train_records)} 条")
    print(f"[{val_out.name}] : 验证集 {len(val_records)} 条")
    print(f"[{Path(args.report_file).name}] : 数据报告已保存")
    print("\n这些数据将直接喂给 GRPO/DAPO，通过 ORM 与格式奖励判断最终答案对错，引导模型自我进化。")

if __name__ == "__main__":
    main()
