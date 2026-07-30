import argparse
import os
from dataclasses import dataclass
from typing import Any

import torch
from datasets import load_dataset
from peft import LoraConfig, get_peft_model
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
    set_seed,
)


@dataclass
class SFTDataCollator:
    """动态 padding，同时保证 prompt 部分的 labels 保持为 -100。"""

    tokenizer: Any

    def __call__(self, features: list[dict[str, Any]]) -> dict[str, torch.Tensor]:
        input_ids = [feature["input_ids"] for feature in features]
        attention_mask = [feature["attention_mask"] for feature in features]
        labels = [feature["labels"] for feature in features]

        padded_inputs = self.tokenizer.pad(
            {"input_ids": input_ids, "attention_mask": attention_mask},
            padding=True,
            return_tensors="pt",
        )
        padded_labels = self.tokenizer.pad(
            {"input_ids": labels},
            padding=True,
            return_tensors="pt",
        )["input_ids"]
        padded_labels[padded_labels == self.tokenizer.pad_token_id] = -100
        padded_inputs["labels"] = padded_labels
        return padded_inputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MedGPT-o1 SFT 训练脚本")
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        required=True,
        help="预训练模型路径，例如 Qwen2.5-7B-Instruct 的本地路径。",
    )
    parser.add_argument("--train_file", type=str, required=True, help="SFT 训练集 jsonl。")
    parser.add_argument("--val_file", type=str, required=True, help="SFT 验证集 jsonl。")
    parser.add_argument("--output_dir", type=str, default="./outputs/sft_model", help="模型输出路径。")
    parser.add_argument("--max_seq_length", type=int, default=4096, help="最大序列长度。")
    parser.add_argument("--per_device_train_batch_size", type=int, default=1, help="单卡 batch size。")
    parser.add_argument("--per_device_eval_batch_size", type=int, default=1, help="单卡验证 batch size。")
    parser.add_argument("--gradient_accumulation_steps", type=int, default=8, help="梯度累积步数。")
    parser.add_argument("--learning_rate", type=float, default=2e-5, help="学习率。")
    parser.add_argument("--num_train_epochs", type=float, default=2.0, help="训练轮数。")
    parser.add_argument("--logging_steps", type=int, default=5, help="日志打印频率。")
    parser.add_argument("--eval_steps", type=int, default=50, help="验证频率。")
    parser.add_argument("--save_steps", type=int, default=100, help="保存频率。")
    parser.add_argument("--save_total_limit", type=int, default=3, help="最多保留多少个 checkpoint。")
    parser.add_argument("--lora_rank", type=int, default=16, help="LoRA 秩。")
    parser.add_argument("--lora_alpha", type=int, default=32, help="LoRA alpha。")
    parser.add_argument("--lora_dropout", type=float, default=0.05, help="LoRA dropout。")
    parser.add_argument("--seed", type=int, default=42, help="随机种子。")
    parser.add_argument("--report_to", type=str, default="none", help="日志上报目标，默认 none；可设为 wandb。")
    parser.add_argument("--wandb_project", type=str, default="MedGPT-o1", help="wandb 项目名。")
    parser.add_argument("--wandb_run_name", type=str, default=None, help="wandb 本次运行名称。")
    parser.add_argument("--gradient_checkpointing", action="store_true", help="开启梯度检查点以降低显存占用。")
    return parser.parse_args()


def build_tokenize_fn(tokenizer: Any, max_seq_length: int):
    def tokenize_example(example: dict[str, Any]) -> dict[str, Any]:
        messages = example["messages"]
        if len(messages) < 2 or messages[-1]["role"] != "assistant":
            raise ValueError(f"样本 {example.get('id', '<unknown>')} 的 messages 格式不符合 SFT 要求。")

        prompt_messages = messages[:-1]
        full_text = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=False,
        )
        prompt_text = tokenizer.apply_chat_template(
            prompt_messages,
            tokenize=False,
            add_generation_prompt=True,
        )

        full_tokens = tokenizer(
            full_text,
            truncation=True,
            max_length=max_seq_length,
            add_special_tokens=False,
        )
        prompt_tokens = tokenizer(
            prompt_text,
            truncation=True,
            max_length=max_seq_length,
            add_special_tokens=False,
        )

        labels = list(full_tokens["input_ids"])
        prompt_len = min(len(prompt_tokens["input_ids"]), len(labels))
        labels[:prompt_len] = [-100] * prompt_len

        if all(label == -100 for label in labels):
            labels[-1] = full_tokens["input_ids"][-1]

        return {
            "input_ids": full_tokens["input_ids"],
            "attention_mask": full_tokens["attention_mask"],
            "labels": labels,
        }

    return tokenize_example


def main() -> None:
    args = parse_args()
    set_seed(args.seed)
    if args.report_to == "wandb":
        os.environ.setdefault("WANDB_PROJECT", args.wandb_project)
        if args.wandb_run_name:
            os.environ.setdefault("WANDB_NAME", args.wandb_run_name)

    print(f"加载 tokenizer：{args.model_name_or_path}")
    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path, trust_remote_code=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
        tokenizer.pad_token_id = tokenizer.eos_token_id
    tokenizer.padding_side = "right"

    print("加载 SFT 数据集。")
    raw_datasets = load_dataset(
        "json",
        data_files={"train": args.train_file, "validation": args.val_file},
    )
    print(f"训练样本：{len(raw_datasets['train'])} 条；验证样本：{len(raw_datasets['validation'])} 条。")

    tokenize_fn = build_tokenize_fn(tokenizer, args.max_seq_length)
    tokenized_datasets = raw_datasets.map(
        tokenize_fn,
        remove_columns=raw_datasets["train"].column_names,
        desc="将 messages 转换为 SFT token",
    )

    print(f"加载模型：{args.model_name_or_path}")
    model = AutoModelForCausalLM.from_pretrained(
        args.model_name_or_path,
        torch_dtype=torch.bfloat16,
        trust_remote_code=True,
    )
    model.config.use_cache = False
    if args.gradient_checkpointing:
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})

    print("配置 LoRA。")
    peft_config = LoraConfig(
        r=args.lora_rank,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, peft_config)
    model.print_trainable_parameters()

    training_args = TrainingArguments(
        output_dir=args.output_dir,
        per_device_train_batch_size=args.per_device_train_batch_size,
        per_device_eval_batch_size=args.per_device_eval_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate,
        num_train_epochs=args.num_train_epochs,
        logging_steps=args.logging_steps,
        eval_strategy="steps",
        eval_steps=args.eval_steps,
        save_strategy="steps",
        save_steps=args.save_steps,
        save_total_limit=args.save_total_limit,
        bf16=True,
        gradient_checkpointing=args.gradient_checkpointing,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        report_to=[] if args.report_to == "none" else [args.report_to],
        remove_unused_columns=False,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized_datasets["train"],
        eval_dataset=tokenized_datasets["validation"],
        data_collator=SFTDataCollator(tokenizer),
    )

    print("开始 SFT 训练。")
    trainer.train()

    print(f"保存 LoRA adapter 和 tokenizer 到：{args.output_dir}")
    os.makedirs(args.output_dir, exist_ok=True)
    trainer.save_model(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    print("SFT 训练完成。")


if __name__ == "__main__":
    main()
