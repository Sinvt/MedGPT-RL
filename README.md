# MedGPT-o1

MedGPT-o1 is a Chinese medical o1-style reasoning project built on Qwen2.5-7B-Instruct. It implements an end-to-end post-training pipeline for medical reasoning: data construction, supervised fine-tuning, GRPO reinforcement learning, LLM-Judge reward modeling, vLLM-assisted rollout, and multi-level evaluation.

> This repository focuses on the training pipeline, reward design, evaluation code, and experiment reports. Large datasets, model checkpoints, and generated rollout caches are intentionally excluded from Git.

## Highlights

- Built a four-stage medical reasoning data pipeline: MCQ -> OpenQA -> Complex CoT -> RFT.
- Constructed a 6.8K teacher CoT SFT-v1 dataset and a 9.2K SFT-v2-A dataset by combining teacher-distilled CoT with 2,372 self-verified RFT trajectories.
- Implemented LoRA SFT and GRPO training with TRL, PEFT, Transformers, and PyTorch.
- Designed a V3 composite reward for Chinese medical RLVR: format constraints, exact-match shortcut, and MiMo LLM-Judge semantic scoring.
- Integrated vLLM for high-throughput rollout generation and built cache/rate-limit safeguards for external Judge calls.
- Built a three-level evaluation suite: internal OpenQA/ORM, public medical benchmarks, and custom Chinese medical tasks for CMB/CMExam.
- Achieved a CMExam full-evaluation improvement from 75.54% to 80.56% (+5.02pp) relative to the base model.

## Project Structure

```text
MedGPT-o1-Main/
+-- configs/              # training and runtime configs
+-- prompts/              # prompts for OpenQA conversion, CoT generation, and judging
+-- scripts/              # setup, model merge, export, and utility scripts
+-- src/
|   +-- data_pipeline/    # MCQ -> OpenQA -> CoT -> RFT -> RL/DAPO data builders
|   +-- training/         # SFT, GRPO, and DAPO-lite training entrypoints
|   +-- rewards/          # hard constraints, MiMo Judge, and composite reward
|   +-- evaluation/       # inference, internal ORM eval, lm-eval custom tasks
+-- tests/                # reward and judge tests
+-- reports/              # compact experiment reports and evaluation summaries
+-- implementation_plan.md
+-- PROJECT_LOG.md
+-- README.md
```

## Pipeline Overview

```text
80K Chinese medical MCQs
-> MCQ-to-OpenQA rewriting
-> Complex CoT teacher distillation
-> ORM and rule-based filtering
-> SFT-v1 data
-> RFT rejection sampling with vLLM
-> SFT-v2-A data
-> GRPO / DAPO-lite RLVR training
-> internal ORM + public benchmark + CMB/CMExam evaluation
```

## Core Components

### Data Engineering

The data pipeline converts multiple-choice Chinese medical exam questions into open-ended, verifiable medical reasoning questions. It standardizes answers and aliases, generates Complex CoT traces with a teacher model, filters low-quality samples with ORM and local rules, and uses vLLM-based rejection sampling to mine self-verified student CoT.

Key outputs from the completed experiments:

| Stage | Output |
| --- | ---: |
| Teacher CoT SFT-v1 | 6.8K samples |
| RFT candidate CoT | 42,096 generations |
| Strict ORM RFT positives | 2,372 samples |
| SFT-v2-A | 9.2K samples |

### Training and Reward Modeling

The project uses LoRA-based SFT and GRPO training rather than full-parameter fine-tuning. The GRPO pipeline starts from an SFT policy, performs online sampling, computes rewards, estimates advantages, and updates the policy through TRL's `GRPOTrainer`.

The V3 reward model addresses sparse hard-ORM rewards in medical short-answer tasks:

```text
invalid format        -> -0.25
exact final answer    -> 2.15
medical contradiction -> 0.00
semantic non-exact    -> 0.15 + 1.70 * semantic_score
```

This design improves reward separability for semantically equivalent medical answers while preserving strict exact-match shortcuts for unambiguous cases.

### Evaluation

The project separates evaluation into three layers:

| Layer | Purpose |
| --- | --- |
| Internal OpenQA/ORM | Fast training diagnostics for answer extraction, format, and reward behavior |
| Public benchmarks | MMLU-Pro, CMMLU, MedQA, MedMCQA, PubMedQA, GSM8K |
| Chinese medical tasks | Custom CMB and CMExam tasks based on lm-evaluation-harness |

For Chinese medical multiple-choice evaluation, the repository includes custom answer extraction and normalization logic to support single-choice and multi-choice outputs, avoiding invalid zero-score artifacts from overly narrow regex parsing.

## Results Snapshot

Chinese medical evaluation is the most relevant evaluation layer for this project because the training data is primarily Chinese medical exam data.

| Model | CMB | CMExam |
| --- | ---: | ---: |
| Qwen2.5-7B-Instruct Base | 71.43% | 75.54% |
| SFT-v1 | 70.71% | 80.36% |
| GRPO-v1 | 71.43% | 80.44% |
| SFT-v2-A | 70.00% | 80.52% |
| GRPO-V3 | 70.36% | 80.56% |

The strongest and most stable result is the CMExam improvement from 75.54% to 80.56% (+5.02pp). Public benchmark changes are more mixed and should be interpreted as local improvements and stability checks rather than a broad claim of across-the-board benchmark gains.

## Quick Start

Install dependencies:

```bash
cd MedGPT-o1-Main
pip install -r requirements.txt
```

Run data construction scripts in order:

```bash
python src/data_pipeline/01_mcq_to_openqa.py
python src/data_pipeline/02_complex_cot_gen.py
python src/data_pipeline/02_5_cot_filtering.py
python src/data_pipeline/03_build_sft_dataset.py
python src/data_pipeline/04_build_rl_dataset.py
```

Train SFT:

```bash
python src/training/train_sft.py \
  --base_model /path/to/Qwen2.5-7B-Instruct \
  --train_file data/final/sft/sft_train.jsonl \
  --val_file data/final/sft/sft_val.jsonl \
  --output_dir outputs/sft_qwen2_5_7b_lora_v1
```

Train GRPO:

```bash
python src/training/train_grpo.py \
  --base_model /path/to/Qwen2.5-7B-Instruct \
  --sft_lora_path outputs/sft_v2_a_lora \
  --train_file data/final/rl_v3/rl_clean_train.jsonl \
  --val_file data/final/rl_v3/rl_clean_val.jsonl \
  --output_dir outputs/grpo_qwen2_5_7b_v3
```

Run Chinese medical evaluation:

```bash
bash src/evaluation/run_eval_zh_med.sh grpo_v3
```

## Notes

- Model weights, optimizer states, raw data, generated datasets, rollout caches, and local environment installers are ignored by Git.
- MiMo Judge calls require API credentials and rate-limit configuration in the runtime environment.
- GRPO-V3 is best described as a successful RLVR engineering and reward-design experiment with limited but measurable Chinese medical benchmark gains, not as a universal public-benchmark breakthrough.
