# MedGPT-o1 Main

This directory contains the main implementation of MedGPT-o1, a Chinese medical o1-style reasoning post-training project based on Qwen2.5-7B-Instruct.

## What This Project Does

MedGPT-o1 builds a complete medical reasoning post-training workflow:

```text
Chinese medical MCQs
-> OpenQA conversion
-> Complex CoT distillation
-> SFT data construction
-> RFT rejection sampling
-> GRPO / DAPO-lite RLVR training
-> multi-level evaluation
```

The project is intended as a reproducible engineering pipeline for exploring Chinese medical reasoning, reward design, and RLVR training diagnostics.

## Main Results

| Component | Result |
| --- | ---: |
| Teacher CoT SFT-v1 data | 6.8K samples |
| RFT candidate CoT generations | 42,096 |
| Strict ORM RFT positives | 2,372 |
| SFT-v2-A data | 9.2K samples |
| CMExam Base -> GRPO-V3 | 75.54% -> 80.56% |

## Directory Layout

```text
configs/              Runtime and training configs
prompts/              Prompt templates for data construction and judging
scripts/              Environment setup, model merge, export, and utility scripts
src/data_pipeline/    Data construction, RFT, RL-v3 cleaning, DAPO-lite data builders
src/training/         SFT, GRPO, DAPO-lite training entrypoints
src/rewards/          Hard constraints, MiMo Judge, composite rewards
src/evaluation/       Inference, internal ORM eval, lm-eval custom tasks
tests/                Unit tests for reward and judge logic
reports/              Experiment and evaluation summaries
```

## Data Pipeline

The data pipeline has four main stages:

1. `MCQ -> OpenQA`: convert Chinese medical multiple-choice questions into open-ended, verifiable questions.
2. `OpenQA -> Complex CoT`: generate teacher reasoning traces.
3. `Filtering`: use ORM, format checks, and local rules to remove low-quality outputs.
4. `RFT`: use vLLM rejection sampling to mine self-verified student CoT trajectories.

Representative scripts:

```bash
python src/data_pipeline/01_mcq_to_openqa.py
python src/data_pipeline/02_complex_cot_gen.py
python src/data_pipeline/02_5_cot_filtering.py
python src/data_pipeline/03_build_sft_dataset.py
python src/data_pipeline/04_build_rl_dataset.py
python src/data_pipeline/05_rft_rejection_sampling.py
python src/data_pipeline/06_rft_deduplication.py
python src/data_pipeline/07_clean_rl_conflicts.py
```

## Training

The project uses LoRA-based training:

- SFT-v1: teacher CoT supervised fine-tuning.
- GRPO-v1: first RLVR attempt with local hard rewards.
- SFT-v2-A: RFT-augmented supervised fine-tuning.
- GRPO-V3: reward-upgraded GRPO with semantic LLM Judge.
- DAPO-lite: effective-gradient sample filtering based on reward variance.

SFT:

```bash
python src/training/train_sft.py \
  --base_model /path/to/Qwen2.5-7B-Instruct \
  --train_file data/final/sft/sft_train.jsonl \
  --val_file data/final/sft/sft_val.jsonl \
  --output_dir outputs/sft_qwen2_5_7b_lora_v1
```

GRPO:

```bash
python src/training/train_grpo.py \
  --base_model /path/to/Qwen2.5-7B-Instruct \
  --sft_lora_path outputs/sft_v2_a_lora \
  --train_file data/final/rl_v3/rl_clean_train.jsonl \
  --val_file data/final/rl_v3/rl_clean_val.jsonl \
  --output_dir outputs/grpo_qwen2_5_7b_v3
```

## Reward Design

GRPO-V3 uses a layered composite reward:

```text
invalid format        -> -0.25
exact final answer    -> 2.15
medical contradiction -> 0.00
semantic non-exact    -> 0.15 + 1.70 * semantic_score
```

Core files:

```text
src/rewards/hard_constraints.py
src/rewards/llm_judge.py
src/rewards/composite_reward.py
tests/test_llm_judge.py
```

## Evaluation

Evaluation has three layers:

1. Internal OpenQA/ORM evaluation for format, answer extraction, and reward diagnostics.
2. Public medical benchmarks through lm-evaluation-harness.
3. Custom Chinese medical CMB/CMExam tasks with relaxed answer extraction and multi-choice normalization.

Chinese medical evaluation:

```bash
bash src/evaluation/run_eval_zh_med.sh grpo_v3
```

Public benchmark evaluation:

```bash
bash src/evaluation/run_eval_all.sh grpo_v3
```

## Reproducibility Notes

- Large datasets, generated data, rollout caches, model weights, and optimizer states are excluded from Git.
- Use `PROJECT_LOG.md` for the full experiment timeline and `implementation_plan.md` for the original plan.
- Some scripts expect cloud paths such as `/gemini/pretrain/Qwen2.5-7B-Instruct`; adjust paths before running locally.
- MiMo Judge requires API credentials and runtime rate-limit settings.

