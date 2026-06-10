# Medical-GPT 医疗大模型全流程开源项目

本项目展示了一个完整的垂直领域大模型训练与评测闭环。包含了从**数据构建**到**模型训练**，再到**自动化评测**的完整工作流。基于此套流程，可以快速复现并改进微调模型的性能，最终打造出一个具备强大医疗知识与推理能力的“赛博医生”。

## 架构说明

本项目分为三个核心模块，对应大模型工程师在业务落地时的三大核心链路：

### 📁 1. 数据构建 (`1_Data_Construction/`)
数据是模型的基础。本目录用于存放数据爬取、清洗、格式化转换、向量化匹配召回以及偏好数据构造的脚本。
- **思路参考**：`HealthAI-2025` 数据构造方法。
- **目标**：为 SFT（监督微调）和 PPO（强化学习）阶段提供高质量、格式对齐的数据源。

### 📁 2. 模型训练 (`2_Model_Training/`)
核心炼丹炉。包含完整的 `SFT -> Reward Model -> PPO` 三阶段训练管线代码及执行脚本。
- 基于开源项目 `MedicalGPT` 进行深度适配和超参调优。
- 支持 `Qwen` 等主流大模型基座的 LoRA/QLoRA 训练。
- 提供终端及 Web 网页（Gradio）交互测试脚本。

### 📁 3. 模型评测 (`3_Evaluation/`)
模型的考官。使用业内公认的 `lm-evaluation-harness` 评测框架。
- 支持加载合并后的权重（Base+LoRA）或直接挂载 Adapter 进行测试。
- 可对中文医疗榜单（如 `ceval-valid` 的医疗子项、`cmmlu`、`medmcqa` 等）进行打分。
- **目标**：用量化的分数取代主观判断，为每一步数据迭代和算法优化提供可靠的证据链。

## 快速开始

进入各个子模块，查看其内部的具体运行说明。
例如，要启动网页版对话测试：
```bash
cd 2_Model_Training
python demo/gradio_demo.py --base_model merged-sft-qwen-0.5b --lora_model outputs-ppo-qwen-0.5b
```

## 面试与项目展示指北
*”不跑一遍，面试一定露馅”* —— 本项目不仅跑通了代码，更打通了数据流向与评估验证的闭环。你可以清晰地阐述自己如何通过清洗某批数据，使得 C-Eval 医疗得分提高了 xxx，以及解决了训练过程中由于 Batch Size 或学习率导致的 Loss 抖动问题。

## 致谢

本项目在开发过程中参考和使用了以下优秀的开源项目，在此表示诚挚的感谢：

- **[HealthAI-2025](https://github.com/yuandaxia2001/HealthAI-2025)** — 数据构造思路借鉴了该项目在临床医学数据处理中的向量检索筛选与知识蒸馏方法，为本项目的数据构建管线提供了重要参考。
- **[MedicalGPT](https://github.com/shibing624/MedicalGPT)** — 项目基于 MedicalGPT（Apache License 2.0）进行深度适配和超参调优，其完善的 SFT / RLHF / DPO 训练框架是模型训练模块的核心基础。
- **[lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness)** — 使用 EleutherAI 开源的评测框架（MIT License）作为模型评测的统一基准，实现了对多维度医学场景的客观量化评估。

各项目的代码版权及开源协议归其原作者所有。
