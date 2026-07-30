# MedGPT-o1：中文医疗复杂推理后训练项目

MedGPT-o1 是一个基于 **Qwen2.5-7B-Instruct** 的中文医疗 o1-style 推理项目。项目围绕医疗复杂推理场景，独立实现了从数据工程、监督微调（SFT）、GRPO 强化学习、复合奖励函数设计、vLLM Rollout 加速，到多层评测验证的完整后训练闭环。

> 本仓库主要保留训练代码、奖励函数、评测实现与实验报告。原始数据、生成数据、模型权重、优化器状态和 Rollout 缓存体积较大，未提交至 Git。

## 项目亮点

- 构建 `MCQ -> OpenQA -> Complex CoT -> RFT` 四阶段中文医疗推理数据管线。
- 从 80K 中文医学选择题出发，构造 6.8K 条 teacher CoT SFT-v1 数据。
- 基于 vLLM 生成 42,096 条候选学生 CoT，经严格 ORM 筛选得到 2,372 条 RFT 正样本，并融合构建 9.2K 条 SFT-v2-A 数据。
- 基于 TRL、PEFT/LoRA、Transformers 和 PyTorch 完成 SFT 与 GRPO 多轮训练实验。
- 针对硬 ORM 奖励稀疏、组内 Advantage 退化问题，设计“格式约束 + Exact Match 短路 + LLM Judge 连续语义评分”的 V3 分层复合奖励。
- 接入 vLLM 作为高吞吐 Rollout 后端，并实现外部 Judge 调用的缓存、限流与异常保护。
- 搭建内部 OpenQA/ORM、公开医学基准、中文医疗 CMB/CMExam 三层评测体系。
- 在 CMExam 6,811 题全量评测中，准确率由 Base 的 75.54% 提升至 80.56%（+5.02pp）。

## 项目结构

```text
MedGPT-o1-Main/
+-- configs/              # 训练与运行配置
+-- prompts/              # OpenQA 改写、CoT 生成、LLM Judge 提示词
+-- scripts/              # 环境配置、模型合并、导出与辅助脚本
+-- src/
|   +-- data_pipeline/    # MCQ -> OpenQA -> CoT -> RFT -> RL 数据构造
|   +-- training/         # SFT、GRPO 与 DAPO-lite 训练入口
|   +-- rewards/          # 硬约束、MiMo Judge 与复合奖励函数
|   +-- evaluation/       # 推理、内部 ORM 评测与 lm-eval 自定义任务
+-- tests/                # 奖励函数与 Judge 逻辑测试
+-- reports/              # 实验记录和评测结果摘要
+-- implementation_plan.md
+-- PROJECT_LOG.md
+-- README.md
```

## 技术路线

```text
80K 中文医学选择题
-> MCQ-to-OpenQA 题型改写
-> Teacher Complex CoT 蒸馏
-> ORM 与本地规则过滤
-> SFT-v1
-> 基于 vLLM 的 RFT Rejection Sampling
-> SFT-v2-A
-> GRPO / DAPO-lite RLVR 训练
-> 内部 ORM + 公开基准 + CMB/CMExam 评测
```

## 数据工程

数据管线将中文医学选择题改写为可验证的开放式问答，完成答案及同义表达归一化、Teacher Complex CoT 蒸馏、ORM 与本地规则质量过滤，并通过基于 vLLM 的 Rejection Sampling 挖掘模型能够自验证的学生推理轨迹。

| 阶段 | 数据规模 |
| --- | ---: |
| Teacher CoT SFT-v1 | 6.8K |
| RFT 候选 CoT | 42,096 |
| 严格 ORM 筛选后的 RFT 样本 | 2,372 |
| 融合后的 SFT-v2-A | 9.2K |

## 训练实验演进

项目采用 LoRA 参数高效微调，主要经历四轮实验：

| 实验 | 初始化模型 | 目标 |
| --- | --- | --- |
| SFT-v1 | Qwen2.5-7B-Instruct | 学习 Teacher CoT 的中文医疗推理与输出格式 |
| GRPO-v1 | SFT-v1 | 验证基于硬 ORM 的 RLVR 训练闭环 |
| SFT-v2-A | SFT-v1 | 融合 Teacher CoT 与 RFT 学生轨迹 |
| GRPO-V3 | SFT-v2-A | 使用连续语义奖励改善组内奖励可分性 |

GRPO-v1 暴露出明显的训练信号问题：大量同题采样组内四个回答获得相同奖励，导致 `frac_reward_zero_std` 偏高，组内 Advantage 不足。GRPO-V3 因此将硬匹配奖励升级为分层复合奖励，提高语义等价答案之间的奖励可分性。

## 奖励函数设计

GRPO-V3 使用三级判定：

```text
格式不合法          -> -0.25
最终答案精确匹配    ->  2.15
存在医学结论矛盾    ->  0.00
非精确但语义合理    ->  0.15 + 1.70 * semantic_score
```

其中，确定性规则负责格式和精确答案校验，LLM Judge 只处理无法通过 Exact Match 判断的语义等价回答。该设计兼顾了判定效率、确定性与医学短答案的语义容错能力。

## 评测体系

| 层级 | 作用 |
| --- | --- |
| 内部 OpenQA/ORM | 快速检查答案抽取、格式正确性和奖励行为 |
| 公开医学基准 | MedQA、MedMCQA、PubMedQA、CMMLU、MMLU-Pro 等 |
| 中文医疗评测 | 基于 lm-evaluation-harness 自定义 CMB 与 CMExam 任务 |

中文选择题评测实现了单选、多选答案的抽取与归一化，避免因正则表达式过严造成无效零分。公开基准结果存在波动，因此本项目不将局部提升描述为全面能力突破，核心量化结果以与训练领域最匹配的 CMExam 为主。

## 结果汇总

| 模型 | CMB | CMExam |
| --- | ---: | ---: |
| Qwen2.5-7B-Instruct Base | 71.43% | 75.54% |
| SFT-v1 | 70.71% | 80.36% |
| GRPO-v1 | 71.43% | 80.44% |
| SFT-v2-A | 70.00% | 80.52% |
| GRPO-V3 | 70.36% | **80.56%** |

最稳定的结果是 CMExam 准确率从 75.54% 提升至 80.56%，绝对提升 5.02 个百分点。该结果说明数据工程与后训练流程对同分布中文医疗考试任务有效；同时，GRPO 相对 SFT 的增益较小，仍需要结合更高质量的困难样本和更稳定的在线奖励继续验证。

## 快速开始

安装依赖：

```bash
cd MedGPT-o1-Main
pip install -r requirements.txt
```

按顺序运行数据构造：

```bash
python src/data_pipeline/01_mcq_to_openqa.py
python src/data_pipeline/02_complex_cot_gen.py
python src/data_pipeline/02_5_cot_filtering.py
python src/data_pipeline/03_build_sft_dataset.py
python src/data_pipeline/04_build_rl_dataset.py
```

训练 SFT：

```bash
python src/training/train_sft.py \
  --base_model /path/to/Qwen2.5-7B-Instruct \
  --train_file data/final/sft/sft_train.jsonl \
  --val_file data/final/sft/sft_val.jsonl \
  --output_dir outputs/sft_qwen2_5_7b_lora_v1
```

训练 GRPO-V3：

```bash
python src/training/train_grpo.py \
  --base_model /path/to/Qwen2.5-7B-Instruct \
  --sft_lora_path outputs/sft_v2_a_lora \
  --train_file data/final/rl_v3/rl_clean_train.jsonl \
  --val_file data/final/rl_v3/rl_clean_val.jsonl \
  --output_dir outputs/grpo_qwen2_5_7b_v3
```

运行中文医疗评测：

```bash
bash src/evaluation/run_eval_zh_med.sh grpo_v3
```

## 复现说明

- 数据集、模型权重、优化器状态、Rollout 缓存和本地环境安装包均通过 `.gitignore` 排除。
- 部分脚本保留了云端训练路径，例如 `/gemini/pretrain/Qwen2.5-7B-Instruct`，运行前需替换为本地路径。
- MiMo Judge 依赖外部 API，需要在运行环境中配置凭据、请求限速和缓存目录。
- 完整实验演进见 [PROJECT_LOG.md](MedGPT-o1-Main/PROJECT_LOG.md)，核心实现说明见 [MedGPT-o1-Main/README.md](MedGPT-o1-Main/README.md)。
