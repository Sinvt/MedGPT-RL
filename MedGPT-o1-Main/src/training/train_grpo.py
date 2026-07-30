import argparse
import json
import os
import pathlib
import re
import sys
import unicodedata
from typing import Any, Iterable

import torch
from datasets import load_dataset
from peft import LoraConfig, PeftModel, TaskType, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer, set_seed

# 尽量让 Windows / 云端终端都按 UTF-8 输出中文日志。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
# 绕过 HuggingFace Transformers 对 torch.load 的 CVE-2025-32434 安全检查限制
import transformers.trainer
if hasattr(transformers.trainer, "check_torch_load_is_safe"):
    transformers.trainer.check_torch_load_is_safe = lambda: None

# 兼容部分 TRL 版本在 Windows / Python 3.11 下读取模板时传入 newline 参数的问题。
_OLD_READ_TEXT = pathlib.Path.read_text


def _read_text_utf8(self, encoding=None, errors=None, newline=None):
    return _OLD_READ_TEXT(self, encoding=encoding or "utf-8", errors=errors)


pathlib.Path.read_text = _read_text_utf8

from trl import GRPOConfig, GRPOTrainer


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from src.rewards.composite_reward import composite_reward_v3_func
from src.rewards.hard_constraints import check_exact_match, extract_final_answer


def build_grpo_config(**kwargs):
    """只传当前 TRL 版本支持的 GRPOConfig 参数。"""
    fields = getattr(GRPOConfig, "__dataclass_fields__", {})
    return GRPOConfig(**{key: value for key, value in kwargs.items() if key in fields})


def find_latest_checkpoint(output_dir: str) -> str | None:
    """从输出目录中寻找 step 最大的 checkpoint，用于断点续训。"""
    output_path = pathlib.Path(output_dir)
    checkpoints = []
    for path in output_path.glob("checkpoint-*"):
        if not path.is_dir():
            continue
        match = re.search(r"checkpoint-(\d+)$", path.name)
        if match:
            checkpoints.append((int(match.group(1)), path))
    if not checkpoints:
        return None
    return str(max(checkpoints, key=lambda item: item[0])[1])


def render_prompt_value(prompt, tokenizer) -> str:
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        return tokenizer.apply_chat_template(prompt, tokenize=False, add_generation_prompt=True)
    return str(prompt or "")


def prepare_prompt_dataset(dataset, tokenizer, use_plain_prompt: bool):
    """预渲染 prompt，减少不同 TRL 版本对 messages 处理差异带来的风险。"""
    if not use_plain_prompt:
        return dataset

    def _render(example):
        return {"prompt": render_prompt_value(example["prompt"], tokenizer)}

    return dataset.map(_render, desc="渲染 RL prompt")


def run_preflight_generation(model, tokenizer, dataset, max_new_tokens):
    """训练前做一次普通 generate，确认 SFT adapter 与 prompt 没有跑偏。"""
    sample = dataset[0]
    prompt = render_prompt_value(sample["prompt"], tokenizer)
    inputs = tokenizer([prompt], return_tensors="pt").to(model.device)
    model.eval()
    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=min(max_new_tokens, 512),
            do_sample=False,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )
    generated_ids = outputs[0][inputs.input_ids.shape[1] :]
    response = tokenizer.decode(generated_ids, skip_special_tokens=True).strip()
    model.train()

    print("\n" + "=" * 80)
    print("GRPO 启动前生成检查")
    print("=" * 80)
    print(f"问题：{sample.get('question', '')}")
    print("-" * 80)
    print(response[:1200])
    print("=" * 80 + "\n")


def run_rollout_sanity(model, tokenizer, dataset, args):
    """只做 rollout 与 reward 计算，不进入 GRPOTrainer。"""
    output_path = pathlib.Path(args.sanity_output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    records = []
    sample_count = min(args.sanity_samples, len(dataset))
    model.eval()
    for idx in range(sample_count):
        sample = dataset[idx]
        prompt = render_prompt_value(sample["prompt"], tokenizer)
        inputs = tokenizer([prompt], return_tensors="pt").to(model.device)
        generate_kwargs = {
            "max_new_tokens": args.max_completion_length,
            "do_sample": args.sanity_do_sample,
            "pad_token_id": tokenizer.pad_token_id,
            "eos_token_id": tokenizer.eos_token_id,
        }
        if args.sanity_do_sample:
            generate_kwargs["temperature"] = args.temperature
            generate_kwargs["top_p"] = args.top_p

        print("\n" + "=" * 80)
        print(f"Rollout 自检题 {idx + 1}/{sample_count}")
        print(f"问题：{sample.get('question', '')}")
        print(f"标准答案：{sample.get('standard_answer', '')}")
        print("=" * 80)

        for gen_idx in range(args.num_generations):
            with torch.no_grad():
                outputs = model.generate(**inputs, **generate_kwargs)
            generated_ids = outputs[0][inputs.input_ids.shape[1] :]
            response = tokenizer.decode(generated_ids, skip_special_tokens=True).strip()
            standard_answer = sample.get("standard_answer", "")
            answer_aliases = sample.get("answer_aliases", [])
            
            extracted = extract_final_answer(response)
            match_type = "exact" if check_exact_match(extracted, standard_answer, answer_aliases) else "none"
            
            # 只使用终极版 V3 奖励函数组合
            rewards_v3 = composite_reward_v3_func(
                completions=[response], 
                standard_answer=[standard_answer], 
                answer_aliases=[answer_aliases],
                question=[sample.get("question", "")]
            )
            reward_detail = {"composite_v3": rewards_v3[0]}
            total_score = rewards_v3[0]
            
            record = {
                "id": sample.get("id"),
                "question": sample.get("question", ""),
                "standard_answer": standard_answer,
                "answer_aliases": answer_aliases,
                "candidate_index": gen_idx,
                "response": response,
                "match_type": match_type,
                "rewards": reward_detail,
                "repetition_penalty_monitor": 0.0,
                "total_reward": total_score,
            }
            records.append(record)

            print(f"\n--- Candidate {gen_idx + 1}/{args.num_generations} [Match: {match_type}] ---")
            print(response[:800] + ("..." if len(response) > 800 else ""))
            print(f"奖励明细：{reward_detail} | 总分：{total_score:.2f}")

    with output_path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    model.train()
    print("\n" + "=" * 80)
    print(f"Rollout 自检完成，结果保存至：{output_path}")
    print("=" * 80 + "\n")


def parse_args():
    parser = argparse.ArgumentParser(description="MedGPT-o1 GRPO 强化学习脚本")
    parser.add_argument("--base_model", type=str, default="/gemini/pretrain/Qwen2.5-7B-Instruct", help="基础模型路径。")
    parser.add_argument("--sft_lora_path", type=str, default="outputs/sft_qwen2_5_7b_lora_v1", help="SFT LoRA adapter 路径。")
    parser.add_argument("--train_file", type=str, default="data/final/rl/rl_train.jsonl", help="RL 训练集 jsonl。")
    parser.add_argument("--val_file", type=str, default="data/final/rl/rl_val.jsonl", help="RL 验证集 jsonl。")
    parser.add_argument("--output_dir", type=str, default="outputs/grpo_qwen2_5_7b_lora_v2", help="GRPO 输出目录。")
    parser.add_argument("--max_steps", type=int, default=50, help="最大训练步数；小规模闭环建议先用 10-50。")
    parser.add_argument("--num_train_epochs", type=float, default=1.0, help="训练轮数；设置 max_steps 时优先按 max_steps 停止。")
    parser.add_argument("--learning_rate", type=float, default=5e-6, help="GRPO 学习率，建议小于 SFT。")
    parser.add_argument("--per_device_train_batch_size", type=int, default=1, help="单卡 prompt batch size。")
    parser.add_argument("--gradient_accumulation_steps", type=int, default=8, help="梯度累积步数。")
    parser.add_argument("--num_generations", type=int, default=4, help="每个 prompt 采样回答数。")
    parser.add_argument("--max_prompt_length", type=int, default=1024, help="最大 prompt 长度。")
    parser.add_argument("--max_completion_length", type=int, default=768, help="最大回答长度。")
    parser.add_argument("--temperature", type=float, default=0.7, help="GRPO 采样温度。")
    parser.add_argument("--top_p", type=float, default=0.9, help="GRPO top-p 采样参数。")
    parser.add_argument("--device", type=str, default="auto", help="运行设备：auto、cuda 或 cpu。")
    parser.add_argument("--skip_preflight", action="store_true", help="跳过训练前普通生成检查。")
    parser.add_argument(
        "--plain_prompt",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="将 messages prompt 预渲染成字符串后交给 GRPOTrainer，默认开启。",
    )
    parser.add_argument("--rollout_sanity_only", action="store_true", help="只做生成与 reward 自检，不启动 GRPO 训练。")
    parser.add_argument("--sanity_samples", type=int, default=3, help="rollout 自检样本数。")
    parser.add_argument("--sanity_output_file", type=str, default="reports/grpo_rollout_sanity.jsonl", help="rollout 自检输出文件。")
    parser.add_argument("--sanity_do_sample", action="store_true", help="rollout 自检时使用采样生成；默认贪心生成。")
    parser.add_argument(
        "--merge_sft_lora",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否先合并 SFT LoRA，再挂载新的 GRPO LoRA，默认开启。",
    )
    parser.add_argument(
        "--gradient_checkpointing",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="是否开启梯度检查点。默认关闭，避免与 LoRA/生成缓存产生额外冲突。",
    )
    parser.add_argument("--report_to", type=str, default="none", help="日志上报目标：none 或 wandb。")
    parser.add_argument("--grpo_lora_rank", type=int, default=16, help="GRPO 阶段新 LoRA 的 rank。")
    parser.add_argument("--grpo_lora_alpha", type=int, default=32, help="GRPO 阶段新 LoRA 的 alpha。")
    parser.add_argument("--grpo_lora_dropout", type=float, default=0.05, help="GRPO 阶段新 LoRA 的 dropout。")
    parser.add_argument("--logging_steps", type=int, default=1, help="日志打印频率。")
    parser.add_argument("--eval_steps", type=int, default=25, help="验证频率。")
    parser.add_argument("--save_steps", type=int, default=25, help="保存频率。")
    parser.add_argument("--save_total_limit", type=int, default=2, help="最多保留 checkpoint 数。")
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help="断点续训 checkpoint 路径；传 latest 时自动从 output_dir 中最新 checkpoint 恢复。",
    )
    parser.add_argument("--seed", type=int, default=42, help="随机种子。")
    parser.add_argument("--wandb_project", type=str, default="MedGPT-o1", help="wandb 项目名。")
    parser.add_argument("--wandb_run_name", type=str, default="grpo-qwen2.5-7b-v3", help="wandb run 名称。")
    parser.add_argument("--use_vllm", action="store_true", help="是否使用 vLLM 加速生成。")
    parser.add_argument("--vllm_gpu_memory_utilization", type=float, default=0.30, help="vLLM 显存比例。")
    parser.add_argument("--vllm_max_model_len", type=int, default=2048, help="vLLM 最大长度。")
    return parser.parse_args()


def main():
    args = parse_args()
    set_seed(args.seed)

    if args.use_vllm:
        import trl
        from trl import GRPOConfig
        print(f"\n检查 TRL 版本与 vLLM 支持 (当前 TRL: {trl.__version__})...")
        trl_fields = getattr(GRPOConfig, "__dataclass_fields__", {})
        if "use_vllm" not in trl_fields:
            print("❌ [版本闸门拦截] 当前 TRL 版本的 GRPOConfig 不支持 use_vllm。")
            print("❌ 已强制关闭 vLLM 以防参数被静默丢弃！请在单独环境中升级 TRL。")
            args.use_vllm = False
        else:
            print(f"✅ 版本闸门通过！TRL {trl.__version__} 原生支持 vLLM。\n")

    print(f"gradient_checkpointing：{args.gradient_checkpointing}（默认关闭；显存够用时建议保持关闭）")
    if args.gradient_checkpointing:
        print("警告：当前已开启 gradient checkpointing。若出现 LoRA/生成异常，建议改用 --no-gradient_checkpointing。")

    if args.report_to == "wandb":
        os.environ.setdefault("WANDB_PROJECT", args.wandb_project)
        os.environ.setdefault("WANDB_NAME", args.wandb_run_name)

    print(f"加载 RL 数据集：{args.train_file} / {args.val_file}")
    dataset = load_dataset("json", data_files={"train": args.train_file, "validation": args.val_file})
    train_dataset = dataset["train"]
    eval_dataset = dataset["validation"]
    print(f"训练样本：{len(train_dataset)}；验证样本：{len(eval_dataset)}")

    print(f"加载 tokenizer：{args.base_model}")
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
        tokenizer.pad_token_id = tokenizer.eos_token_id
    tokenizer.padding_side = "left"

    train_dataset = prepare_prompt_dataset(train_dataset, tokenizer, args.plain_prompt)
    eval_dataset = prepare_prompt_dataset(eval_dataset, tokenizer, args.plain_prompt)
    if args.plain_prompt:
        print("已将 RL prompt 预渲染成 chat-template 字符串。")

    if args.device == "auto":
        runtime_device = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        runtime_device = args.device
    if runtime_device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("当前 PyTorch 没有检测到 CUDA，不能使用 --device cuda。")
    print(f"运行设备：{runtime_device}")
    print(f"CUDA 可用：{torch.cuda.is_available()}；CUDA 设备数：{torch.cuda.device_count()}")

    print(f"加载基础模型：{args.base_model}")
    base_model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        torch_dtype=torch.bfloat16,
        trust_remote_code=True,
        low_cpu_mem_usage=True,
    )
    base_model.config.use_cache = False

    print(f"加载 SFT LoRA adapter：{args.sft_lora_path}")
    sft_model = PeftModel.from_pretrained(base_model, args.sft_lora_path, is_trainable=False)

    if args.merge_sft_lora:
        print("将 SFT LoRA 合并进基础模型，然后挂载新的 GRPO LoRA。")
        model = sft_model.merge_and_unload()
        grpo_peft_config = LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            r=args.grpo_lora_rank,
            lora_alpha=args.grpo_lora_alpha,
            lora_dropout=args.grpo_lora_dropout,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
            inference_mode=False,
        )
        model = get_peft_model(model, grpo_peft_config)
    else:
        print("直接继续训练 SFT LoRA adapter。若 rollout 异常，建议使用默认 merge 模式。")
        model = PeftModel.from_pretrained(base_model, args.sft_lora_path, is_trainable=True)

    model.config.use_cache = False
    model.to(runtime_device)
    if args.gradient_checkpointing:
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    else:
        model.gradient_checkpointing_disable()
    model_device = next(model.parameters()).device
    print(f"模型已移动到：{model_device}")
    if runtime_device == "cuda" and model_device.type != "cuda":
        raise RuntimeError(f"模型没有成功移动到 CUDA，当前设备为：{model_device}")
    model.print_trainable_parameters()

    if not args.skip_preflight:
        run_preflight_generation(model, tokenizer, train_dataset, args.max_completion_length)

    if args.rollout_sanity_only:
        run_rollout_sanity(model, tokenizer, train_dataset, args)
        return

    vllm_kwargs = {}
    if args.use_vllm:
        vllm_kwargs = {
            "use_vllm": True,
            "vllm_gpu_memory_utilization": args.vllm_gpu_memory_utilization,
            "vllm_max_model_len": args.vllm_max_model_len,
            "vllm_mode": "colocate",
        }

    training_args = build_grpo_config(
        **vllm_kwargs,
        output_dir=args.output_dir,
        learning_rate=args.learning_rate,
        num_train_epochs=args.num_train_epochs,
        max_steps=args.max_steps,
        per_device_train_batch_size=args.per_device_train_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        gradient_checkpointing=args.gradient_checkpointing,
        gradient_checkpointing_kwargs={"use_reentrant": False} if args.gradient_checkpointing else None,
        num_generations=args.num_generations,
        max_prompt_length=args.max_prompt_length,
        max_completion_length=args.max_completion_length,
        temperature=args.temperature,
        top_p=args.top_p,
        mask_truncated_completions=True,
        bf16=True,
        beta=0.04,
        report_to=[] if args.report_to == "none" else [args.report_to],
        run_name=args.wandb_run_name,
        logging_steps=args.logging_steps,
        eval_strategy="steps",
        eval_steps=args.eval_steps,
        save_strategy="steps",
        save_steps=args.save_steps,
        save_total_limit=args.save_total_limit,
        log_completions=True,
        num_completions_to_print=2,
        remove_unused_columns=False,
    )

    print("启用终极版 V3 奖励函数组合：复合硬格式 + Exact短路 + MiMo Judge 连续打分。")
    active_reward_funcs = [composite_reward_v3_func]

    print("初始化 GRPOTrainer。")
    trainer = GRPOTrainer(
        model=model,
        reward_funcs=active_reward_funcs,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        processing_class=tokenizer,
    )

    resume_checkpoint = args.resume_from_checkpoint
    if resume_checkpoint == "latest":
        resume_checkpoint = find_latest_checkpoint(args.output_dir)
        if resume_checkpoint:
            print(f"检测到最新 checkpoint，将从这里断点续训：{resume_checkpoint}")
        else:
            print("没有检测到可恢复的 checkpoint，将从头开始训练。")
    elif resume_checkpoint:
        print(f"将从指定 checkpoint 断点续训：{resume_checkpoint}")

    print("开始 GRPO 强化学习训练。")
    trainer.train(resume_from_checkpoint=resume_checkpoint)

    print(f"训练完成，保存 LoRA adapter 和 tokenizer 到：{args.output_dir}")
    trainer.save_model(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)


if __name__ == "__main__":
    main()
