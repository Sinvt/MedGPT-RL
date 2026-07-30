#!/usr/bin/env python3
"""DAPO-inspired GRPO training entrypoint.

This script keeps the validated V3 reward path and runs a DAPO-lite experiment
without depending on a specific native DAPO implementation. If the installed
TRL build exposes DAPOConfig/DAPOTrainer, `--trainer_backend auto` uses it. In
the common TRL 0.19.x environment, it safely falls back to GRPOTrainer while
preserving the DAPO-inspired parts that are available in this project:

- data-level dynamic sampling via a pre-filtered effective-gradient dataset;
- truncated-completion masking when supported by the installed trainer config;
- optional asymmetric clipping / DAPO config knobs when supported by TRL;
- the V3 medical reward path with exact-match short circuit and MiMo Judge.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
from pathlib import Path
from typing import Any, Iterable

import torch
from datasets import load_dataset
from peft import LoraConfig, PeftModel, TaskType, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.rewards.composite_reward import composite_reward_v3_func


DAPO_INSPIRED_CONFIG_KEYS = {
    "mask_truncated_completions",
    "scale_rewards",
    "epsilon",
    "epsilon_high",
    "loss_type",
    "dynamic_sampling",
    "use_dynamic_sampling",
    "filter_groups_without_reward_std",
}


def patch_transformers_safety_check() -> None:
    """Bypass a conservative transformers check that rejects some remote code."""

    try:
        from transformers.dynamic_module_utils import get_class_in_module as original
        import transformers.dynamic_module_utils as dmu

        def safe_get_class_in_module(class_name, module_path, *args, **kwargs):
            try:
                return original(class_name, module_path, *args, **kwargs)
            except ValueError as exc:
                if "got multiple values for argument" in str(exc):
                    import importlib.util
                    import sys

                    name = Path(module_path).stem
                    spec = importlib.util.spec_from_file_location(name, module_path)
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[name] = module
                    spec.loader.exec_module(module)
                    return getattr(module, class_name)
                raise

        dmu.get_class_in_module = safe_get_class_in_module
    except Exception as exc:  # pragma: no cover - defensive runtime patch
        print(f"transformers safety patch skipped: {exc}")


def patch_path_read_text() -> None:
    """Make Path.read_text tolerate omitted encodings on Chinese workspaces."""

    original_read_text = Path.read_text

    def safe_read_text(self, *args, **kwargs):
        if "encoding" not in kwargs and len(args) == 0:
            kwargs["encoding"] = "utf-8"
        return original_read_text(self, *args, **kwargs)

    Path.read_text = safe_read_text


def ensure_single_rank_env() -> None:
    """Provide the single-process distributed env expected by vLLM V1."""

    os.environ.setdefault("RANK", "0")
    os.environ.setdefault("LOCAL_RANK", "0")
    os.environ.setdefault("WORLD_SIZE", "1")
    os.environ.setdefault("MASTER_ADDR", "127.0.0.1")
    os.environ.setdefault("MASTER_PORT", "29500")


def patch_vllm_sampling_params() -> None:
    """Drop TRL/vLLM version-skew kwargs before SamplingParams sees them."""

    try:
        from vllm import SamplingParams

        original_init = SamplingParams.__init__
        if getattr(original_init, "_medgpt_patched", False):
            return

        def patched_init(self, *args, **kwargs):
            kwargs.pop("guided_decoding", None)
            kwargs.pop("guided_decoding_regex", None)
            return original_init(self, *args, **kwargs)

        patched_init._medgpt_patched = True
        SamplingParams.__init__ = patched_init
    except Exception as exc:  # pragma: no cover - optional dependency
        print(f"vLLM SamplingParams patch skipped: {exc}")


def load_trainer_classes(backend: str):
    from trl import GRPOConfig, GRPOTrainer

    try:
        from trl import DAPOConfig, DAPOTrainer
    except Exception:
        DAPOConfig = None
        DAPOTrainer = None

    if backend == "native_dapo":
        if DAPOConfig is None or DAPOTrainer is None:
            raise RuntimeError(
                "The installed TRL package does not expose DAPOConfig/DAPOTrainer. "
                "Use --trainer_backend auto or upgrade TRL to a DAPO-capable build."
            )
        return DAPOConfig, DAPOTrainer, "native_dapo"

    if backend == "grpo_compat":
        return GRPOConfig, GRPOTrainer, "grpo_compat"

    if DAPOConfig is not None and DAPOTrainer is not None:
        return DAPOConfig, DAPOTrainer, "native_dapo"

    return GRPOConfig, GRPOTrainer, "grpo_compat"


def supported_kwargs(config_cls: type, **kwargs: Any) -> dict[str, Any]:
    fields = set()
    for cls in inspect.getmro(config_cls):
        fields.update(getattr(cls, "__dataclass_fields__", {}).keys())
    return {key: value for key, value in kwargs.items() if key in fields}


def skipped_kwargs(config_cls: type, **kwargs: Any) -> list[str]:
    fields = set()
    for cls in inspect.getmro(config_cls):
        fields.update(getattr(cls, "__dataclass_fields__", {}).keys())
    return [key for key in kwargs if key not in fields]


def trainer_supported_fields(config_cls: type) -> set[str]:
    fields = set()
    for cls in inspect.getmro(config_cls):
        fields.update(getattr(cls, "__dataclass_fields__", {}).keys())
    return fields


def is_effective_dataset_path(path: str | Path) -> bool:
    name = str(path).lower()
    return "dapo" in name or "effective" in name


def write_run_metadata(
    output_dir: str | Path,
    *,
    args: argparse.Namespace,
    backend_name: str,
    config_cls: type,
    trainer_cls: type,
    supported_config: dict[str, Any],
    skipped_config_keys: list[str],
    train_size: int,
    eval_size: int,
) -> None:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    supported_dapo_keys = sorted(DAPO_INSPIRED_CONFIG_KEYS.intersection(supported_config))
    skipped_dapo_keys = sorted(DAPO_INSPIRED_CONFIG_KEYS.intersection(skipped_config_keys))
    metadata = {
        "experiment": args.experiment_label,
        "positioning": (
            "DAPO-lite / DAPO-inspired GRPO. This is not claimed to be a full "
            "native DAPO optimizer unless backend_name is native_dapo."
        ),
        "backend_name": backend_name,
        "config_class": config_cls.__name__,
        "trainer_class": trainer_cls.__name__,
        "train_file": args.train_file,
        "val_file": args.val_file,
        "train_size": train_size,
        "eval_size": eval_size,
        "base_model": args.base_model,
        "sft_lora_path": args.sft_lora_path,
        "starts_from_sft_v2_a": bool(args.sft_lora_path and args.sft_lora_path.lower() != "none"),
        "data_level_dynamic_sampling": is_effective_dataset_path(args.train_file),
        "reward_function": "composite_reward_v3_func",
        "supported_dapo_inspired_config_keys": supported_dapo_keys,
        "skipped_dapo_inspired_config_keys": skipped_dapo_keys,
        "skipped_all_config_keys": skipped_config_keys,
        "token_level_loss_note": (
            "Token-level loss was intentionally not reimplemented in this final run "
            "to avoid trainer-internal risk under a fixed A100/API budget."
        ),
    }
    (output_path / "dapo_lite_run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def print_dapo_lite_plan(
    *,
    args: argparse.Namespace,
    backend_name: str,
    config_cls: type,
    trainer_cls: type,
    supported_config: dict[str, Any],
    skipped_config_keys: list[str],
) -> None:
    supported_dapo_keys = sorted(DAPO_INSPIRED_CONFIG_KEYS.intersection(supported_config))
    skipped_dapo_keys = sorted(DAPO_INSPIRED_CONFIG_KEYS.intersection(skipped_config_keys))

    print("\n" + "=" * 72)
    print("DAPO-lite / DAPO-inspired GRPO plan")
    print("=" * 72)
    print(f"Experiment label: {args.experiment_label}")
    print(f"Backend: {backend_name} ({config_cls.__name__}, {trainer_cls.__name__})")
    print(f"Train file: {args.train_file}")
    print(f"Starts from SFT adapter: {args.sft_lora_path}")
    print(f"Data-level dynamic sampling: {'yes' if is_effective_dataset_path(args.train_file) else 'not detected from file name'}")
    print("Enabled by project design:")
    print("- Effective-gradient prompt filtering, if train_file is produced by 06_build_dapo_effective_dataset.py")
    print("- V3 medical reward: hard format gate + exact-match shortcut + MiMo semantic Judge")
    print("- Truncated/overlong defense through trainer masking when supported and reward format penalties")
    print("Trainer-supported DAPO-inspired config keys:")
    print("- " + (", ".join(supported_dapo_keys) if supported_dapo_keys else "<none>"))
    if skipped_dapo_keys:
        print("DAPO-inspired keys skipped by this TRL build:")
        print("- " + ", ".join(skipped_dapo_keys))
    print("Token-level loss: intentionally not hand-reimplemented for this final resource-limited run.")
    if backend_name == "grpo_compat":
        print("Interpretation: this run is DAPO-inspired GRPO, not a full native DAPO optimizer.")
    print("=" * 72 + "\n")


def latest_checkpoint(output_dir: str | Path) -> str | None:
    path = Path(output_dir)
    if not path.exists():
        return None

    checkpoints = []
    for child in path.glob("checkpoint-*"):
        if not child.is_dir():
            continue
        try:
            step = int(child.name.rsplit("-", 1)[1])
        except ValueError:
            continue
        checkpoints.append((step, child))

    if not checkpoints:
        return None
    return str(max(checkpoints, key=lambda item: item[0])[1])


def render_prompt_value(example: dict[str, Any], tokenizer, plain_prompt: bool) -> str:
    if not plain_prompt and "prompt" in example:
        prompt = example["prompt"]
        if isinstance(prompt, list):
            return tokenizer.apply_chat_template(
                prompt,
                tokenize=False,
                add_generation_prompt=True,
            )
        if isinstance(prompt, str):
            return prompt

    question = example.get("question") or example.get("problem") or example.get("query")
    if not question:
        question = str(example)

    messages = [
        {
            "role": "system",
            "content": (
                "你是一个严谨的中文医学推理助手。请先在 <think>...</think> "
                "中写出必要推理过程，再用 `最终答案：` 给出简洁答案。"
            ),
        },
        {"role": "user", "content": str(question)},
    ]
    return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)


def prepare_prompt_dataset(raw_dataset, tokenizer, plain_prompt: bool):
    def convert(example):
        out = dict(example)
        out["prompt"] = render_prompt_value(example, tokenizer, plain_prompt)
        return out

    return raw_dataset.map(convert)


def run_preflight_generation(model, tokenizer, dataset, max_new_tokens: int = 256) -> None:
    sample = dataset[0]
    print("\n" + "=" * 72)
    print("DAPO preflight generation")
    print("=" * 72)
    print(f"Question: {sample.get('question', '')}")
    inputs = tokenizer(sample["prompt"], return_tensors="pt").to(model.device)
    with torch.no_grad():
        generated = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )
    new_tokens = generated[0][inputs["input_ids"].shape[-1] :]
    print(tokenizer.decode(new_tokens, skip_special_tokens=False))
    print("=" * 72 + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train DAPO-lite / DAPO-inspired GRPO with V3 reward.")

    parser.add_argument("--base_model", default="/gemini/pretrain/Qwen2.5-7B-Instruct")
    parser.add_argument("--sft_lora_path", default="outputs/sft_v2_a_lora")
    parser.add_argument("--train_file", default="data/final/dapo_lite/dapo_effective_train.jsonl")
    parser.add_argument("--val_file", default="data/final/dapo_lite/dapo_effective_val.jsonl")
    parser.add_argument("--output_dir", default="outputs/dapo_lite_qwen2_5_7b_v3")

    parser.add_argument("--trainer_backend", choices=["auto", "native_dapo", "grpo_compat"], default="auto")
    parser.add_argument("--experiment_label", default="dapo_lite_dynamic_sampling_grpo")
    parser.add_argument("--dapo_epsilon_low", type=float, default=0.2)
    parser.add_argument("--dapo_epsilon_high", type=float, default=0.28)
    parser.add_argument("--dapo_loss_type", default="dapo")
    parser.add_argument("--dapo_mask_truncated_completions", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dapo_scale_rewards", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--dapo_dynamic_sampling", action=argparse.BooleanOptionalAction, default=True)

    parser.add_argument("--max_steps", type=int, default=-1)
    parser.add_argument("--num_train_epochs", type=float, default=1.0)
    parser.add_argument("--learning_rate", type=float, default=5e-6)
    parser.add_argument("--per_device_train_batch_size", type=int, default=2)
    parser.add_argument("--gradient_accumulation_steps", type=int, default=8)
    parser.add_argument("--num_generations", type=int, default=4)
    parser.add_argument("--max_prompt_length", type=int, default=1024)
    parser.add_argument("--max_completion_length", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--top_p", type=float, default=0.9)

    parser.add_argument("--logging_steps", type=int, default=1)
    parser.add_argument("--save_steps", type=int, default=100)
    parser.add_argument("--eval_steps", type=int, default=100)
    parser.add_argument("--save_total_limit", type=int, default=3)
    parser.add_argument("--report_to", default="wandb")
    parser.add_argument("--wandb_project", default="MedGPT-o1")
    parser.add_argument("--wandb_run_name", default="dapo-lite-qwen2.5-7b-v3-judge")
    parser.add_argument("--seed", type=int, default=1234)

    parser.add_argument("--gradient_checkpointing", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--plain_prompt", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--merge_sft_lora", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--skip_preflight", action="store_true")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--resume_from_checkpoint", default=None)

    parser.add_argument("--dapo_lora_rank", type=int, default=32)
    parser.add_argument("--dapo_lora_alpha", type=int, default=64)
    parser.add_argument("--dapo_lora_dropout", type=float, default=0.05)

    parser.add_argument("--use_vllm", action="store_true")
    parser.add_argument("--vllm_gpu_memory_utilization", type=float, default=0.30)
    parser.add_argument("--vllm_max_model_len", type=int, default=None)

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    patch_transformers_safety_check()
    patch_path_read_text()
    if args.use_vllm:
        ensure_single_rank_env()
        patch_vllm_sampling_params()

    os.environ.setdefault("WANDB_PROJECT", args.wandb_project)
    os.environ.setdefault("WANDB_NAME", args.wandb_run_name)

    config_cls, trainer_cls, backend_name = load_trainer_classes(args.trainer_backend)
    print(f"Trainer backend: {backend_name} ({config_cls.__name__}, {trainer_cls.__name__})")
    if backend_name == "grpo_compat":
        print("Warning: native DAPO is unavailable; running DAPO-inspired GRPO-compatible backend.")

    raw_data = load_dataset(
        "json",
        data_files={"train": args.train_file, "validation": args.val_file},
    )

    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True, use_fast=False)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    train_dataset = prepare_prompt_dataset(raw_data["train"], tokenizer, args.plain_prompt)
    eval_dataset = prepare_prompt_dataset(raw_data["validation"], tokenizer, args.plain_prompt)
    print(f"Train samples: {len(train_dataset)}; validation samples: {len(eval_dataset)}")
    if not is_effective_dataset_path(args.train_file):
        print(
            "Warning: train_file name does not look like an effective-gradient DAPO-lite dataset. "
            "For the final run, prefer data/final/dapo_lite/dapo_effective_train.jsonl."
        )

    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        torch_dtype=torch.bfloat16,
        trust_remote_code=True,
        low_cpu_mem_usage=True,
    )
    model.config.use_cache = False

    if args.sft_lora_path and args.sft_lora_path.lower() != "none":
        print(f"Loading SFT LoRA adapter: {args.sft_lora_path}")
        model = PeftModel.from_pretrained(model, args.sft_lora_path)
        if args.merge_sft_lora:
            print("Merging SFT LoRA into base model before attaching DAPO LoRA.")
            model = model.merge_and_unload()
    else:
        print("Skipping SFT LoRA adapter load (base model is presumably already merged).")

    dapo_lora_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=args.dapo_lora_rank,
        lora_alpha=args.dapo_lora_alpha,
        lora_dropout=args.dapo_lora_dropout,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        bias="none",
    )
    model = get_peft_model(model, dapo_lora_config)
    if args.gradient_checkpointing:
        model.gradient_checkpointing_enable()
    model.to(args.device)
    model.print_trainable_parameters()

    if not args.skip_preflight:
        run_preflight_generation(model, tokenizer, train_dataset)

    base_config = {
        "output_dir": args.output_dir,
        "learning_rate": args.learning_rate,
        "per_device_train_batch_size": args.per_device_train_batch_size,
        "gradient_accumulation_steps": args.gradient_accumulation_steps,
        "num_train_epochs": args.num_train_epochs,
        "max_steps": args.max_steps,
        "max_prompt_length": args.max_prompt_length,
        "max_completion_length": args.max_completion_length,
        "num_generations": args.num_generations,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "logging_steps": args.logging_steps,
        "save_steps": args.save_steps,
        "eval_steps": args.eval_steps,
        "save_total_limit": args.save_total_limit,
        "eval_strategy": "steps",
        "save_strategy": "steps",
        "report_to": args.report_to,
        "bf16": True,
        "gradient_checkpointing": args.gradient_checkpointing,
        "remove_unused_columns": False,
        "log_completions": True,
        "num_completions_to_print": 2,
        "seed": args.seed,
        "beta": 0.04,
        "mask_truncated_completions": args.dapo_mask_truncated_completions,
        "scale_rewards": args.dapo_scale_rewards,
        "epsilon": args.dapo_epsilon_low,
        "epsilon_high": args.dapo_epsilon_high,
        "dynamic_sampling": args.dapo_dynamic_sampling,
        "use_dynamic_sampling": args.dapo_dynamic_sampling,
        "filter_groups_without_reward_std": args.dapo_dynamic_sampling,
    }
    if backend_name == "native_dapo":
        base_config["loss_type"] = args.dapo_loss_type

    if args.use_vllm:
        base_config.update(
            {
                "use_vllm": True,
                "vllm_mode": "colocate",
                "vllm_gpu_memory_utilization": args.vllm_gpu_memory_utilization,
                "vllm_max_model_len": args.vllm_max_model_len,
            }
        )

    skipped = skipped_kwargs(config_cls, **base_config)
    if skipped:
        print(f"Skipped unsupported config keys for {config_cls.__name__}: {', '.join(skipped)}")

    supported_config = supported_kwargs(config_cls, **base_config)
    print_dapo_lite_plan(
        args=args,
        backend_name=backend_name,
        config_cls=config_cls,
        trainer_cls=trainer_cls,
        supported_config=supported_config,
        skipped_config_keys=skipped,
    )
    write_run_metadata(
        args.output_dir,
        args=args,
        backend_name=backend_name,
        config_cls=config_cls,
        trainer_cls=trainer_cls,
        supported_config=supported_config,
        skipped_config_keys=skipped,
        train_size=len(train_dataset),
        eval_size=len(eval_dataset),
    )

    training_args = config_cls(**supported_config)

    trainer = trainer_cls(
        model=model,
        reward_funcs=[composite_reward_v3_func],
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        processing_class=tokenizer,
    )

    resume_checkpoint = args.resume_from_checkpoint or latest_checkpoint(args.output_dir)
    if resume_checkpoint:
        print(f"Resuming from checkpoint: {resume_checkpoint}")

    print("Starting DAPO-lite / DAPO-inspired RL training.")
    trainer.train(resume_from_checkpoint=resume_checkpoint)
    trainer.save_model(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    print(f"Training complete. Saved to {args.output_dir}")


if __name__ == "__main__":
    main()
