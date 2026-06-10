# MedGPT-RL：基于 RLHF 的医疗领域大语言模型对齐训练

[![Python 3.8+](https://img.shields.io/badge/Python-3.8%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Sinvt/MedGPT-RL?style=social)](https://github.com/Sinvt/MedGPT-RL)

> 基于 ChatGPT 训练范式，完整实现医疗大模型的 **SFT → Reward Modeling → PPO** 强化学习对齐全流程，并对比 DPO、GRPO 等主流偏好优化方法在医疗场景下的效果差异。

---

## 📖 项目简介

本项目以医疗问答场景为切入点，围绕 **RLHF（Reinforcement Learning from Human Feedback）** 展开，系统性地复现并对比了当前主流的大模型对齐方法。

### 核心目标
- 完整走通 **SFT → RM → PPO** 的经典 RLHF Pipeline
- 对比 **DPO / ORPO / GRPO** 等免 RM 的偏好优化方法与传统 RLHF 的效果差异
- 探索不同对齐策略在医疗安全性（Helpful, Honest, Harmless）维度上的表现

---

## 🏗️ 技术路线

本项目的训练流程遵循 [Andrej Karpathy - State of GPT](https://karpathy.ai/stateofgpt.pdf) 中提出的训练范式：

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Stage 1    │     │  Stage 2    │     │  Stage 3    │     │  Stage 4    │
│  增量预训练  │ ──▶ │  有监督微调  │ ──▶ │  奖励模型   │ ──▶ │  强化学习   │
│  (PT)       │     │  (SFT)      │     │  (RM)       │     │  (PPO)      │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
                                              │
                                              ▼
                                    ┌───────────────────┐
                                    │  平替方案（无需RM） │
                                    │  DPO / ORPO / GRPO│
                                    └───────────────────┘
```

### 各阶段说明

| 阶段 | 名称 | 说明 | 训练脚本 |
|:---:|:---:|------|:---:|
| Stage 1 | **PT** (Continue PreTraining) | 在医疗领域文档上进行增量预训练，让模型适应领域数据分布 | `training/pretraining.py` |
| Stage 2 | **SFT** (Supervised Fine-tuning) | 使用医疗问答指令数据进行有监督微调，对齐指令意图 | `training/supervised_finetuning.py` |
| Stage 3 | **RM** (Reward Modeling) | 基于人类偏好排序数据训练奖励模型，建模 HHH 原则 | `training/reward_modeling.py` |
| Stage 4 | **PPO** (Reinforcement Learning) | 使用 RM 作为奖励信号，通过 PPO 算法优化 SFT 模型的策略 | `training/ppo_training.py` |
| Alt | **DPO** (Direct Preference Optimization) | 无需 RM，直接在偏好数据上优化语言模型 | `training/dpo_training.py` |
| Alt | **GRPO** (Group Relative Policy Optimization) | 纯 RL 方法，通过组内相对奖励信号优化策略 | `training/grpo_training.py` |

---

## 📁 项目结构

```
MedGPT-RL/
├── training/                # 核心训练脚本
│   ├── pretraining.py                 # Stage 1: 增量预训练 (PT)
│   ├── supervised_finetuning.py       # Stage 2: 有监督微调 (SFT)
│   ├── reward_modeling.py             # Stage 3: 奖励模型 (RM)
│   ├── ppo_training.py                # Stage 4: 强化学习 (PPO)
│   ├── dpo_training.py                # 直接偏好优化 (DPO)
│   ├── grpo_training.py               # GRPO
│   ├── orpo_training.py               # ORPO
│   └── template.py                    # 对话模板定义
│
├── scripts/                 # 一键运行脚本 + DeepSpeed 配置
│   ├── run_sft.sh / run_rm.sh / run_ppo.sh / run_dpo.sh / ...
│   └── zero1.json / zero2.json / zero3.json
│
├── data/                    # 训练数据
│   ├── sft/                           # SFT 指令微调数据
│   └── reward/                        # RM/DPO 偏好对比数据
│
├── demo/                    # 推理与部署示例
│   ├── inference.py                   # 命令行推理
│   └── gradio_demo.py                 # Web UI 演示
│
├── tools/                   # 工具脚本
│   ├── merge_peft_adapter.py          # LoRA 权重合并
│   └── model_quant.py                 # 模型量化
│
└── notebooks/               # Colab 端到端教程
    ├── run_training_ppo_pipeline.ipynb # PT+SFT+RM+PPO 全流程
    └── run_training_dpo_pipeline.ipynb # PT+SFT+DPO 全流程
```

---

## 🚀 快速开始

### 环境安装

```bash
git clone https://github.com/Sinvt/MedGPT-RL.git
cd MedGPT-RL
pip install -r requirements.txt
```

### 硬件需求参考

| 训练方法 | 精度 | 7B 模型 | 13B 模型 |
|:---:|:---:|:---:|:---:|
| LoRA | FP16 | 16GB | 32GB |
| QLoRA | INT4 | 6GB | 12GB |

### 训练示例

```bash
# Step 1: 有监督微调 (SFT)
bash scripts/run_sft.sh

# Step 2: 奖励模型训练 (RM)
bash scripts/run_rm.sh

# Step 3: 强化学习训练 (PPO)
bash scripts/run_ppo.sh

# 或者用 DPO 一步到位（无需 RM）
bash scripts/run_dpo.sh
```

---

## 📊 实验结果

> 🚧 实验进行中，后续将补充：
> - 各阶段训练的 loss 曲线
> - SFT vs RLHF vs DPO 在医疗问答上的对比评测
> - 典型 case 分析（好回答 vs 差回答的对比）
> - 安全性维度（HHH）的评估结果

---

## 🔭 后续计划

- [ ] 跑通完整 SFT → RM → PPO Pipeline
- [ ] 实现 DPO 训练并与 PPO 对比
- [ ] 引入医疗安全性评估指标
- [ ] 尝试 GRPO 等新方法
- [ ] 整理实验报告与可视化

---

## 🙏 致谢

本项目基于 [shibing624/MedicalGPT](https://github.com/shibing624/MedicalGPT) 开发，感谢原作者提供的高质量训练框架和数据支持。

核心参考：
- [Andrej Karpathy - State of GPT](https://karpathy.ai/stateofgpt.pdf)
- [DPO: Direct Preference Optimization](https://arxiv.org/pdf/2305.18290.pdf)
- [GRPO: Group Relative Policy Optimization](https://arxiv.org/pdf/2402.03300)

---

## 📄 License

本项目遵循 [Apache License 2.0](LICENSE) 开源协议。
