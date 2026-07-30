# MedGPT-o1 核心实现

本目录包含 MedGPT-o1 的核心代码。项目基于 **Qwen2.5-7B-Instruct**，用于探索中文医疗复杂推理场景下的数据构造、LoRA SFT、GRPO/RLVR、复合奖励建模与自动化评测。

## 核心流程

```text
中文医学选择题
-> OpenQA 改写
-> Complex CoT 蒸馏与过滤
-> SFT-v1
-> RFT Rejection Sampling
-> SFT-v2-A
-> GRPO / DAPO-lite
-> 多层评测
```

## 关键结果

| 项目 | 结果 |
| --- | ---: |
| Teacher CoT SFT-v1 数据 | 6.8K |
| RFT 候选 CoT | 42,096 |
| 严格 ORM 筛选后的 RFT 样本 | 2,372 |
| SFT-v2-A 数据 | 9.2K |
| CMExam：Base -> GRPO-V3 | 75.54% -> 80.56% |

## 目录说明

```text
configs/              训练与运行配置
prompts/              数据构造和 LLM Judge 提示词
scripts/              环境配置、模型合并、导出与辅助脚本
src/data_pipeline/    OpenQA、CoT、RFT 与 RL 数据构造
src/training/         SFT、GRPO、DAPO-lite 训练入口
src/rewards/          硬约束、MiMo Judge 与复合奖励
src/evaluation/       推理、内部 ORM 与 lm-eval 自定义任务
tests/                奖励函数和 Judge 逻辑测试
reports/              实验与评测摘要
```

## 数据管线

数据工程包含四个主要阶段：

1. `MCQ -> OpenQA`：将中文医学选择题改写为可验证的开放式问答。
2. `OpenQA -> Complex CoT`：调用 Teacher 模型生成复杂推理过程。
3. `质量过滤`：使用 ORM、格式检查与本地规则剔除低质量输出。
4. `RFT`：通过 vLLM 批量采样并筛选模型自生成的正确推理轨迹。

主要脚本：

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

## 训练实验

- `SFT-v1`：基于 Teacher CoT 进行第一轮监督微调。
- `GRPO-v1`：使用本地硬奖励验证第一版 RLVR 闭环。
- `SFT-v2-A`：在 SFT-v1 基础上融合 RFT 学生推理轨迹。
- `GRPO-V3`：引入 LLM Judge 连续语义评分，改善奖励稀疏和组内 Advantage 退化。
- `DAPO-lite`：探索依据组内奖励方差筛选有效梯度样本。

训练 SFT：

```bash
python src/training/train_sft.py \
  --base_model /path/to/Qwen2.5-7B-Instruct \
  --train_file data/final/sft/sft_train.jsonl \
  --val_file data/final/sft/sft_val.jsonl \
  --output_dir outputs/sft_qwen2_5_7b_lora_v1
```

训练 GRPO：

```bash
python src/training/train_grpo.py \
  --base_model /path/to/Qwen2.5-7B-Instruct \
  --sft_lora_path outputs/sft_v2_a_lora \
  --train_file data/final/rl_v3/rl_clean_train.jsonl \
  --val_file data/final/rl_v3/rl_clean_val.jsonl \
  --output_dir outputs/grpo_qwen2_5_7b_v3
```

## 奖励函数

GRPO-V3 使用分层复合奖励：

```text
格式不合法          -> -0.25
最终答案精确匹配    ->  2.15
存在医学结论矛盾    ->  0.00
非精确但语义合理    ->  0.15 + 1.70 * semantic_score
```

核心文件：

```text
src/rewards/hard_constraints.py
src/rewards/llm_judge.py
src/rewards/composite_reward.py
tests/test_llm_judge.py
```

确定性规则优先处理格式与 Exact Match；只有无法直接判断的回答才进入 MiMo LLM Judge，从而在降低调用成本的同时，为医学短答案的语义等价表达提供连续奖励。

## 评测体系

项目包含三层评测：

1. 内部 OpenQA/ORM：检查答案抽取、输出格式与奖励行为。
2. 公开医学基准：通过 lm-evaluation-harness 运行 MedQA、MedMCQA、PubMedQA、CMMLU、MMLU-Pro 等任务。
3. 中文医疗评测：自定义 CMB/CMExam 任务，支持单选、多选答案抽取与归一化。

运行中文医疗评测：

```bash
bash src/evaluation/run_eval_zh_med.sh grpo_v3
```

运行公开基准评测：

```bash
bash src/evaluation/run_eval_all.sh grpo_v3
```

## 复现注意事项

- 大规模数据、生成结果、模型权重、优化器状态和 Rollout 缓存未纳入 Git。
- 部分脚本使用云端绝对路径，运行前需要替换模型与数据路径。
- MiMo Judge 需要配置 API 凭据、请求限速和本地缓存。
- 训练时间线和实验结论见 [PROJECT_LOG.md](PROJECT_LOG.md)，原始实现计划见 [implementation_plan.md](implementation_plan.md)。
