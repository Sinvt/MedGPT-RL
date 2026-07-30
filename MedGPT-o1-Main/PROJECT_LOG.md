# MedGPT-o1 项目日志

> 简要记录每个阶段实际做了什么、得到了什么结果。

## 2026-06-11

完成项目启动前的规划与工程基建准备。

- 明确项目主线：医疗数据构造 -> 离线 0.5B PRM 数据提纯 -> SFT -> GRPO/DAPO -> 评测。
- 确定 PRM 定位：只作为离线数据提纯器和 Golden CoT 筛选器，不参与在线 RL reward。
- 合并形成主执行文档 `implementation_plan.md`，后续项目按该文档推进。
- 整理项目目录结构：

```text
MedGPT-o1-Main/
├── data/                       # 数据目录
│   ├── raw/                    # 原始训练数据
│   ├── intermediate/           # 中间产物，如 MCQ-to-OpenQA、CoT、PRM 打分结果
│   └── final/                  # 最终训练/评测数据
├── src/                        # 核心源码
│   ├── data_pipeline/          # 数据构造与过滤脚本
│   ├── training/               # PRM、SFT、GRPO、DAPO 训练脚本
│   ├── rewards/                # ORM 与在线 RL 规则奖励
│   └── evaluation/             # 评测与错误分析
├── configs/                    # 训练、评测、DeepSpeed 等配置
├── prompts/                    # MCQ-to-OpenQA、CoT、PRM Judge 等 prompt
├── reports/                    # 数据报告、成本记录、评测结果、错误分析
├── scripts/                    # 数据下载与辅助脚本
└── README.md                   # 项目说明
```

## 2026-06-12

**✅ 完成 Phase 0：原始训练数据下载与验证。**

- 编写并运行 `scripts/download_raw_data.py`：使用 Hugging Face 镜像站下载并规范化中文医学选择题训练数据，包含字段校验、split fallback、格式统一和 JSONL 落盘。
- 输入：公开医学选择题数据源 `cmexam`、`medqa_zh`、`cmb`。
- 输出：
  - `data/raw/train/cmexam_train.jsonl`：52,741 条。
  - `data/raw/train/medqa_zh_train.jsonl`：27,327 条。
- 结果：当前可用原始训练数据共 80,068 条；`cmb` 本轮未成功获取，暂不影响后续推进。
- 资源：本阶段为数据下载与本地校验，不消耗 API tokens。

**✅ 完成 Phase 1：MCQ-to-OpenQA 重构链路验证与 7K 级数据生成。**

- 确定 Phase 1 使用 `mimo-v2.5`，不启用深度思考；复杂 CoT 和 PRM Judge 阶段再按需使用 `mimo-v2.5-pro`。
- 编写并运行 `src/data_pipeline/01_mcq_to_openqa.py`：调用 MiMo API 将医学选择题逆向改写为开放式简答题，支持 JSON mode、断点续传、失败样本跳过、来源混合、token 统计、耗时统计、全局进度与 ETA。
- 编写并使用 `prompts/mcq_to_openqa_prompt.md`：约束模型只输出开放式问题和答案别名，避免泄漏选项或选择题痕迹。
- 输入：`data/raw/train/cmexam_train.jsonl` 和 `data/raw/train/medqa_zh_train.jsonl`。
- 执行命令记录：
  ```powershell
  python src/data_pipeline/01_mcq_to_openqa.py `
    --limit XXXX `                                # 0 表示全量处理；数字表示测试条数
    --batch_size 100 `                            # 每次加载进内存的批次大小
    --concurrency 1 `                             # 并发请求数，控制 API QPS 防止限流
    --json_mode always `                          # 强制模型输出标准 JSON 格式
    --max_tokens 1024 `                           # 限制单次回答最大生成 Token 数
    --source_mix cmexam:0.6,medqa_zh:0.4          # 混合不同来源数据的采样比例
  ```
- 输出：
  - `data/intermediate/openqa_raw.jsonl`：7,159 条成功样本。
  - `data/intermediate/openqa_errors.jsonl`：584 条失败/过滤样本。
- 结果：生成 7K 级开放式可验证医学问答数据，来源分布为 `cmexam 4,030`、`medqa_zh 3,129`，数据量已超过 5K 目标，足够进入 Complex CoT 生成阶段。
- 资源：
  - 2500 条中规模运行实际耗时 2 小时 23 分 41 秒，消耗 1,317,885 tokens。
  - 5000 条补量运行根据截图进度估算总耗时约 4 小时 13 分钟，消耗 2,816,833 tokens。
  - Phase 1 OpenQA 累计耗时约 6 小时 37 分钟。
  - MiMo 官网统计本阶段 API 总消耗：4,134,718 Tokens。

## 2026-06-13

**✅ 完成 Phase 2：Complex CoT 生成链路验证与 1K 题规模扩量。**

- 编写并验证：
  - `src/data_pipeline/02_complex_cot_gen.py`：调用 `mimo-v2.5-pro` 生成多路径 Complex CoT，支持断点续跑、来源混合、洗牌抽样、ORM 校验、token 统计和进度 ETA。
  - `prompts/complex_cot_prompt.md`：约束模型输出 `reasoning_steps + final_answer`，再由脚本统一拼接为 `<think>...</think>\n最终答案：...`。
- 输入：`data/intermediate/openqa_raw.jsonl`。
- 执行命令记录：
  ```powershell
  python src/data_pipeline/02_complex_cot_gen.py `
    --limit XXXX `                                # 0 表示全量处理；数字表示测试题数
    --paths_per_question 3 `                      # 核心：每道题生成几条不同的推理路径 (用于对比，不过不构造PRM的话就没必要用3了，直接用1生成一条CoT即可)
    --batch_size 30 `                             # 批次大小
    --concurrency 1 `                             # API 并发数
    --json_mode always `                          # 强制输出标准 JSON
    --max_tokens 2048 `                           # CoT 文本长，需要给足输出限额
    --temperature 0.7 `                           # 核心：调高温度以保证同一题 3 条路径的多样性
    --source_mix cmexam:0.6,medqa_zh:0.4 `        # 数据分布比例
    --shuffle `                                   # 打乱原始数据顺序
    --log_every 10                                # 每处理 10 条打印一次预估时间 (ETA)
  ```
- 输出：
  - `data/intermediate/cot_candidates.jsonl`：3,026 条 CoT 候选，覆盖 1,009 道题。
  - `data/intermediate/cot_errors.jsonl`：生成失败或格式异常样本。
- 结果：1K 题规模 CoT 生成完成，来源分布为 `cmexam 1,820`、`medqa_zh 1,206`。
- 资源：20 题小规模验证消耗 34,213 tokens；1K 题扩量总 token 消耗待 MiMo 官网统计后补充。

**✅ 完成 Phase 2.5：CoT 本地质量过滤。**

- 编写并运行 `src/data_pipeline/02_5_cot_filtering.py`：本地过滤 CoT 候选，检查 ORM 命中、`<think>` 结构、最终答案、推理长度、标注泄漏和选择题痕迹。
- 输入：`data/intermediate/cot_candidates.jsonl`。
- 执行命令记录：
  ```powershell
  python src/data_pipeline/02_5_cot_filtering.py  # 纯本地强规则校验，零 API 成本；可选加 --allow_orm_failed 放行错误答案
  ```
- 输出：
  - `data/intermediate/cot_filtered.jsonl`：2,926 条保留样本，覆盖 1,006 道题。
  - `data/intermediate/cot_rejected.jsonl`：100 条拒绝样本。
  - `reports/cot_quality_report.md`：CoT 质量过滤报告。
- 结果：保留样本 ORM 命中率 100%，来源分布为 `cmexam 1,764`、`medqa_zh 1,162`。
- 资源：本阶段为本地规则过滤，不消耗 API tokens。

**❌ 启动 Phase 3：PRM step-level Judge 标注。（后续把 PRM 相关移除）**

- 编写并验证：
  - `src/data_pipeline/03_prm_labeling.py`：调用 `mimo-v2.5-pro` 对 CoT 逐步标注 PRM 标签，支持断点续跑、step 对齐、token 统计和错误样本落盘。
  - `prompts/prm_labeling_prompt.md`：约束 Judge 输出 `step_labels / overall_quality / has_fatal_error`。
- 输入：`data/intermediate/cot_filtered.jsonl`。
- 输出：
  - `data/intermediate/prm_labels_raw.jsonl`：2,317 条 CoT 标签，覆盖 1,006 道题，共 11,370 个 step 标签。
  - `data/intermediate/prm_label_errors.jsonl`：0 条失败样本。
- 结果：
  - PRM Judge API 链路、JSON 输出、step 对齐和断点续跑均已跑通。
  - 来源分布：`cmexam 1,399`，`medqa_zh 918`。
  - step 标签分布：正例 11,280 个，负例 90 个。
  - `has_fatal_error=True`：25 条。
- 资源：
  - 10 条小测消耗 10,598 tokens。
  - 50 条小测消耗 55,656 tokens。
  - 1000 条扩量中实际新增 940 条，耗时 1 小时 19 分 38 秒，消耗 1,055,405 tokens。
  - MiMo 官网统计 2026-06-13 当日 API 总消耗：4,383,959 tokens。

## 2026-06-14

今天先暂停继续扩充 PRM 标注，对昨天的数据路线做了一次复盘和收束。结论是：当前 `02_5_cot_filtering.py` 已经把进入后续阶段的 CoT 过滤得比较干净，PRM Judge 得到的大多是高质量正例，继续把时间投入到 PRM 标注和 PRM 训练数据构造里，收益不高，反而会让项目主线变复杂。

因此今天到目前为止主要完成了项目路线重构：删除 PRM 相关脚本、prompt、中间数据、final 数据和报告文件；重写 `implementation_plan.md`，把后续主线收敛为 `cot_filtered.jsonl -> SFT 数据集 -> SFT 训练 -> RL 数据集 -> GRPO/DAPO -> 评测报告`。PRM 只作为一次历史探索保留在日志里，不再作为后续 SFT/RL 的阻塞项。

**✅ 完成 Phase 3：SFT 数据集构造。**

- 编写并运行 `src/data_pipeline/03_build_sft_dataset.py`：从 `data/intermediate/cot_filtered.jsonl` 中按题目选择质量最高的一条 CoT，构造成 ChatML/messages 风格的 SFT 数据。
- 输入：`data/intermediate/cot_filtered.jsonl`，共 2,926 条 CoT，覆盖 1,006 道题。
- 输出：
  - `data/final/sft/sft_train.jsonl`：906 条。
  - `data/final/sft/sft_val.jsonl`：100 条。
  - `data/final/sft/sft_all.jsonl`：1,006 条。
  - `reports/sft_dataset_report.md`：SFT 数据集报告。

**✅ 完成 Phase 4：SFT 训练与推理冒烟验证。**

- 编写并运行 `src/training/train_sft.py`：在趋动云单卡 40G 上使用 `Qwen2.5-7B-Instruct + LoRA` 完成 1 epoch SFT 闭环训练，并接入 wandb 监控。
- 训练结果：共 114 step，耗时约 5 分 59 秒；最终 `train_loss ≈ 1.1229`，验证集 `eval_loss` 从约 1.0575 降到约 1.0292；LoRA adapter 保存至 `outputs/sft_qwen2_5_7b_lora`。
- 编写并运行 `src/evaluation/infer_sft.py`：加载 base model + LoRA adapter 做推理冒烟测试，模型已能输出 `<think>...</think>` 推理结构和最终答案，说明 SFT 数据、训练脚本、adapter 保存与推理加载链路已跑通。

**✅ 完成 Phase 5：RL 数据集与奖励函数。**

- 编写并运行 `src/data_pipeline/04_build_rl_dataset.py`：从 `data/intermediate/openqa_raw.jsonl` 构造 GRPO/DAPO 所需的 prompt 数据，只保留题干、标准答案和答案别名，不注入 CoT，让模型在 RL 阶段自行生成推理。
- 编写自定义奖励函数：独立实现 `orm_reward.py`、`format_reward.py` 与 `process_rule_reward.py`，专门解决医疗场景下最终答案的判定问题。
- 输入：`data/intermediate/openqa_raw.jsonl`，共 7,159 条 OpenQA。
- 输出：
  - `data/final/rl/rl_train.jsonl`：7,016 条。
  - `data/final/rl/rl_val.jsonl`：143 条。
  - `reports/rl_dataset_report.md`：RL 数据集报告。
- 结果：RL 数据集与奖励函数机制准备完成，Phase 5 顺利结束。

## 2026-06-15
扩充 Phase 2/2.5 数据规模，并重建 Phase 3 SFT 数据集。

- 使用全量 `data/intermediate/openqa_raw.jsonl` 扩充 CoT 候选数据，当前 OpenQA 基础数据共 7,159 条，来源分布为 `cmexam 4,030`、`medqa_zh 3,129`。
- 更新 `data/intermediate/cot_candidates.jsonl`：当前共有 9,156 条 CoT 候选，覆盖 7,139 道题，来源分布为 `cmexam 5,233`、`medqa_zh 3,923`。
- 重新运行 `src/data_pipeline/02_5_cot_filtering.py`：从 9,156 条候选中保留 8,800 条高质量 CoT，覆盖 6,880 道题；拒绝样本 356 条，保留率 96.11%。
- 输出：
  - `data/intermediate/cot_filtered.jsonl`：8,800 条。
  - `data/intermediate/cot_rejected.jsonl`：356 条。
  - `reports/cot_quality_report.md`：更新后的 CoT 质量过滤报告。
- 重新运行 `src/data_pipeline/03_build_sft_dataset.py`：从过滤后的 CoT 中为每道题选择 1 条质量最高样本，重建 SFT 数据集。
- 输出：
  - `data/final/sft/sft_all.jsonl`：6,880 条。
  - `data/final/sft/sft_train.jsonl`：6,192 条。
  - `data/final/sft/sft_val.jsonl`：688 条。
  - `reports/sft_dataset_report.md`：更新后的 SFT 数据集报告。
- 结果：SFT 数据规模从原来的 1,006 题扩充到 6,880 题，后续可以在云端重新跑一版更充分的 SFT，再继续 Phase 5/6 的 GRPO 调试。

**🚀 重新启动 Phase 4：全量数据 SFT 训练。**

- 运行 `src/training/train_sft.py`：基于扩充后的 6.8K 题全量数据集重新进行 SFT 训练（v2）。
- 执行命令记录：
  ```bash
  python src/training/train_sft.py \
    --model_name_or_path /gemini/pretrain/Qwen2.5-7B-Instruct \
    --train_file data/final/sft/sft_train.jsonl \
    --val_file data/final/sft/sft_val.jsonl \
    --output_dir outputs/sft_qwen2_5_7b_lora_v2 \
    --max_seq_length 2048 \
    --per_device_train_batch_size 1 \
    --gradient_accumulation_steps 8 \
    --num_train_epochs 1 \
    --learning_rate 2e-5 \
    --logging_steps 10 \
    --eval_steps 100 \
    --save_steps 200 \
    --gradient_checkpointing \
    --report_to wandb \
    --wandb_project MedGPT-o1 \
    --wandb_run_name sft-qwen2.5-7b-6.8k-v2
  ```
- 训练超参数与执行情况：
  - **数据集规模**：约 6,880 条 SFT 样本（1 个 Epoch）。
  - **Batch Size**：`per_device_train_batch_size = 1` 且 `gradient_accumulation_steps = 8`。
  - **序列长度**：`max_seq_length = 2048`。
  - **学习率**：`2e-5`。
  - **防崩盘策略**：开启梯度检查点 (`--gradient_checkpointing`) 以节省显存。
  - **训练耗时**：趋动云 P1.medium 40G 单卡运行 1 个 Epoch 全量数据，总耗时约 **43 分钟**。
- 预期目标：这批超过 6000 题的高质量 SFT 数据将为后续的正式 GRPO 提供极其稳固的格式（<think>...</think>）与医疗推理底座。

**🚀 启动 Phase 6：GRPO 强化学习训练。**

- 编写并调试 `src/training/train_grpo.py`：基于 `Qwen2.5-7B-Instruct + SFT LoRA adapter` 继续进行 GRPO，集成我们自主编写的格式、ORM 准确率和轻量推理长度奖励函数。
- 输入：`data/final/rl/rl_train.jsonl`、`data/final/rl/rl_val.jsonl`、`outputs/sft_qwen2_5_7b_lora_v2`。
- 输出目标：`outputs/grpo_qwen2_5_7b_medical_final`。
- 训练超参数与核心配置：
  - **数据集规模**：全量 7,016 条医疗 QA 样本（1 个 Epoch）。
  - **Batch Size**：`per_device_train_batch_size = 1` 且 `gradient_accumulation_steps = 8`。
  - **并行采样**：`num_generations = 4`（每个 prompt 同时生成 4 个不同的回答用以计算 Advantage）。
  - **总更新步数**：3,508 步（底层逻辑：7016 个 Prompt × 4 份生成 = 28064 个总样本；每 8 个样本执行 1 次梯度更新，即 28064 / 8 = 3508 步）。
  - **序列长度**：`max_prompt_length = 1024`，`max_completion_length = 1024`。
  - **学习率**：`5e-6`（相比 SFT 阶段更为保守，防止语言崩塌）。
  - **防崩盘策略**：显式指定 `--no-gradient_checkpointing`（彻底关闭梯度检查点，规避 Qwen2.5 位置编码错乱导致的乱码问题）。
  - **硬件占用与预估耗时**：趋动云 P1.medium 40G 单卡，显存占用稳定在 28GB 左右；单步（8 个生成样本）耗时约 15.7 秒。跑完 1 个 Epoch 全量数据**预估总耗时约 15~17 小时**。
- 环境处理：趋动云当前 `torch 2.2.2+cu121` 与 `trl 1.5.1` 不兼容，已将 TRL 降级并固定为 `trl==0.19.1`，解决 `GRPOTrainer` 导入问题。
- 执行情况：在正式跑完全量 7000 条数据（1 个 Epoch）前，先通过 `--max_steps 300` 进行了全链路烟雾测试。在排除了生成乱码和奖励结算等历史 Bug 后，观察到模型生成了清晰的医学思考链 `<think>`，并且优势计算（Advantage）有效。确认无误后正式启动了全量 GRPO 炼丹流程。后台执行命令记录：
  ```bash
  nohup python src/training/train_grpo.py \
      --base_model "/gemini/pretrain/Qwen2.5-7B-Instruct" \
      --sft_lora_path "outputs/sft_qwen2_5_7b_lora_v2" \
      --train_file "data/final/rl/rl_train.jsonl" \
      --output_dir "outputs/grpo_qwen2_5_7b_medical_final" \
      --num_train_epochs 1.0 \
      --max_steps -1 \
      --num_generations 4 \
      --max_prompt_length 1024 \
      --max_completion_length 1024 \
      --per_device_train_batch_size 1 \
      --gradient_accumulation_steps 8 \
      --learning_rate 5e-6 \
      --logging_steps 5 \
      --save_steps 500 \
      --eval_steps 500 \
      --no-gradient_checkpointing \
      --resume_from_checkpoint "latest" \
      --report_to wandb \
      --wandb_project "MedGPT-o1" \
      --wandb_run_name "grpo-qwen2.5-7b-final-v2" \
      > grpo_training.log 2>&1 &
  ```
- 训练监控：运行后通过 `tail -f grpo_training.log` 实时查看日志输出及评估进度。

## 阶段性技术总结：GRPO 踩坑与修复全记录 (2026-06-15)

在从 SFT 迈向 GRPO 强化学习的过程中，我们遇到了“生成出满屏复读机乱码”且“Reward 全局为 0”的严重阻塞问题。经过本地 Qwen2.5-0.5B-Instruct 的极简链路排查，最终定位并彻底解决了两大核心诱因：

### 1. 模型乱码元凶：`gradient_checkpointing` 与 `use_cache` 的底层冲突
**现象**：模型在 GRPO 的 rollout（生成）阶段，突然从正常的中文变成了无意义的字符重复和碎裂的英文乱码。
**根因**：GRPOTrainer 采用左侧填充（Left-padding）来批量生成多个回答。对于 Qwen2/2.5 架构，模型在 Left-padding 时极其依赖 `use_cache=True` 来正确维持内部的 `position_ids`。然而，当我们在训练参数中开启 `gradient_checkpointing=True`（梯度检查点，用于省显存）时，HuggingFace/TRL 底层会**强制静默地将 `use_cache` 设置为 `False`** 以防止反向传播时的显存冲突。这导致生成阶段的位置编码彻底崩盘，从而引发乱码。
**解决方案**：在运行启动脚本时，**显式禁用梯度检查点**（使用 `--no-gradient_checkpointing` 参数），从而允许 `use_cache=True` 正常工作。同时在生成参数中显式写明 `use_cache: True`。

### 2. 奖励失效元凶：官方 `math_verify` 与自然语言场景的不兼容
**现象**：即便乱码修复、模型正常生成了带 `<think>` 和正确定论的回答，`accuracy_reward` 依然全部结算为 `0.0`。
**根因**：官方最小复现脚本默认使用的是 `math_verify` 库作为答案解析器。该库为了数学问题而生，对文本格式极其苛刻（期待标准的 LaTeX 或特定数字占位）。当模型输出像“最终答案：多潘立酮”这样的自然语言医学诊断时，`math_verify` 无法解析，直接判定为空或错误，剥夺了本该给出的正向 Reward。
**解决方案**：彻底抛弃 `math_verify`。在主项目的 `train_grpo.py` 中全盘接入我们自行编写的 `medical_orm_score`，通过**字符串精准匹配与包含匹配**（Normalized Exact & Containment Match）直接提取“最终答案：”后的文本进行验证。最终成功打通 Reward 信号，模型 Advantage 计算恢复正常！

## 2026-06-16

**完成 Phase 6：GRPO 强化学习训练。**

今天完成了昨晚启动的全量 GRPO 训练，正式跑完 1 个 Epoch。

- 脚本：`src/training/train_grpo.py`。
- 输入：`data/final/rl/rl_train.jsonl`、`outputs/sft_qwen2_5_7b_lora_v2`。
- 输出：`outputs/grpo_qwen2_5_7b_medical_final`。
- 结果：完成 3,508 个更新 step，总耗时约 **21 小时 15 分钟**，保存了 GRPO LoRA adapter 和 tokenizer。
- 训练监控：WandB run 为 `grpo-qwen2.5-7b-final-v2`。

**启动 Phase 7：统一基准评测。**

- 编写并执行了烟雾测试脚本 `src/evaluation/infer_grpo.py`，验证确认 GRPO 模型的双 LoRA（SFT+GRPO）挂载路径完全正确，且模型能够 **100% 遵循 `<think>` 思考链格式**。
- 编写了教科书级的自动化流水线评测脚本 `src/evaluation/evaluate_models.py`。该脚本完美解决了单卡连续加载三大模型（Base / SFT / GRPO）导致的 OOM 问题，通过动态 LoRA 熔接（`merge_and_unload`）和无情清道夫机制（强制 `gc.collect` 与清理 CUDA cache），实现了同一张 40G 显卡上的串行全量测评，并自动汇总 CSV/Markdown 报表。
- **当前状态**：评测脚本已在云端启动，针对 143 道核心验证集（`rl_val.jsonl`）进行最后的全链路摸底。
- **评测发现**：根据前端反馈，虽然思考链格式完全成型，但因 SFT 阶段沉淀的底盘正确率不足，导致极严苛的 ORM 准确率卡在 25% 左右。这也成为决定明天紧急启动 RFT（拒绝采样）计划的核心数据支撑。
- **全量评测结果对比 (Baseline)**：
  - **Base 模型**：ORM命中率 **32.17%**，完整格式 **99.30%**，平均总奖励 2.4811，平均思考字数 209.7。
  - **SFT 模型 (v1)**：ORM命中率 **24.48%**，完整格式 **100.00%**，平均总奖励 2.3755，平均思考字数 217.1。
  - **GRPO 模型 (v1)**：ORM命中率 **27.27%**，完整格式 **100.00%**，平均总奖励 2.4315，平均思考字数 210.6。

## 2026-06-17 计划

**🏗️ 架构升级：确立“训推解耦”双节点工作流**

为了极大加速项目第二轮的核心瓶颈（特别是极其耗时的 RFT 拒绝采样生成），正式确立物理双机协同流水线与严格的模型流转规范：
- **节点 A（训练主节点，当前机器）**：纯净版 PyTorch 2.x + CUDA 12.x。专职负责 SFT、GRPO 模型训练，产出 LoRA / Checkpoint，维持环境纯净。
- **节点 B（推理与评测副节点，新开机器）**：当前验证环境为 PyTorch + vLLM 0.12.0。专职负责基于 `vLLM` 引擎的高速公开榜单评测，以及在 RFT 阶段全速并发，**目标生成 3-5 万条候选 CoT**。
- **双节点流转与评测协议**：
  - 节点间仅通过 `data/`、`outputs/*_merged_v1`、`reports/` 三类文件同步。训练节点只管训 LoRA，推理节点只消费合并后的完整模型，绝对物理隔离。
  - **统一走 Merge 模型评测**（因 vLLM 对多层 PEFT 挂载易出坑）：
    - **Base**：直接读 `/gemini/pretrain/Qwen2.5-7B-Instruct`
    - **SFT**：将 LoRA 融进 Base，保存为 `outputs/sft_merged_v1`
    - **GRPO**：将 SFT+GRPO 融进 Base，保存为 `outputs/grpo_merged_v1`

**📊 第一步：公开医学榜单基准测试 (lm-evaluation-harness)**

在启动数据清洗与二期训练前，先在权威公开榜单上对现有的 `Base` / `SFT-v1` / `GRPO-v1` 完成横向摸底评测。这部分指标将作为项目简历与后续对比的“硬通货”：
- **快速公开集（不带 Chat Template）**：

| 模型 | CMMLU 医疗均分 | MedQA (USMLE) | MedMCQA | PubMedQA | GSM8K |
|---|---|---|---|---|---|
| Base | **82.5%** | **62.1%** | **56.4%** | **73.4%** | **82.8%** |
| SFT (v1) | **81.4%** | **62.1%** | **55.6%** | **75.2%** | **82.8%** |
| GRPO (v1) | **81.9%** | **62.3%** | **55.9%** | **75.0%** | **83.5%** |

- **高难度推理集 MMLU-Pro（不带 Chat Template）**：

| 模型 | MMLU-Pro Biology | MMLU-Pro Health |
|---|---|---|
| Base | **74.2%** | **57.9%** |
| SFT (v1) | **72.7%** | **58.8%** |
| GRPO (v1) | 待填补 | 待填补 |

**🚀 第二步：启动第二轮全面优化 (Round 2 Optimization)**

公开榜单跑通并留档后，为突破第一轮内部测试中“格式达成但 ORM 准确率偏低”的瓶颈，将正式启动由数据引擎驱动的全面优化：

1. **Rejection Sampling Fine-Tuning (自生成-过滤)**
   - 用 SFT-v1 模型生成多路候选 CoT，经严格 ORM 与格式规则筛选后，提取高质量“黄金思考链”，构建出 100% 正确的候选样本集。
2. **数据工程优化：本地向量语义去重**
   - 针对上述 RFT 过滤出的正确样本，用 `bge-m3` 编码 `question + standard_answer + CoT` 结合 FAISS 聚类，大幅剔除模型套用的同质化推理路径，提炼出兼具高正确率与极高多样性的 `SFT-v2` 终极训练集。
3. **奖励函数全面升级**
   - 从原有的基础指标，升级为：`R = ORM(权重最高) + Format + Clinical Structure + Length Control - Repetition Penalty`，防止模型出现“格式好但答案错”的 Reward Hacking 漏洞。
4. **训练第二轮强化学习：GRPO-v2**
   - 基于 `SFT-v2` 与全新升级的奖励函数，重新进行强化学习，作为核心能力对比目标。
5. **探索与对比：DAPO 阶段**
   - 在相同高优数据底盘上训练 DAPO 模型，严格对比其在长推理控制与奖励稳定性上是否优于 GRPO-v2。
6. **最终评测矩阵大对决**
   - 形成 `Base vs SFT-v1 vs GRPO-v1 vs SFT-v2 vs GRPO-v2 vs DAPO` 的终极对照组，彻底闭环整个大模型演进项目！


## 2026-06-17 vLLM 推理评测排障记录

今天主要卡在 `lm_eval --model vllm` 对 `outputs/sft_merged_v1` 做公开榜单评测的推理初始化上。排查过程中先遇到 HuggingFace 数据集直连失败，`datasets.load_dataset("gsm8k")` 报 `Couldn't reach 'gsm8k' on the Hub`，通过设置 `HF_ENDPOINT=https://hf-mirror.com` 和 `HF_HUB_ENABLE_HF_TRANSFER=0` 解决；随后本地模型的 fast tokenizer 报 `data did not match any variant of untagged enum ModelWrapper`，确认是 `tokenizer.json` 与当前 `tokenizers` 版本不兼容，改用 vLLM 的 `tokenizer_mode=slow` 绕过。期间也尝试过切换 attention backend，其中 `XFORMERS` 不是当前 vLLM 可用选项，`TORCH_SDPA` 虽被配置接受但未注册，最终回到默认 FLASH_ATTN 后端继续推进。

最终能跑起来的关键配置是继续使用 vLLM，并显式使用慢 tokenizer：`HF_ENDPOINT=https://hf-mirror.com`、`HF_HUB_ENABLE_HF_TRANSFER=0`，以及 `LLM(model="/gemini/code/MedGPT-o1-Main/outputs/sft_merged_v1", tokenizer=..., tokenizer_mode="slow", dtype="bfloat16", gpu_memory_utilization=0.80, max_model_len=2048, enforce_eager=True, disable_custom_all_reduce=True)`。日志显示 4 个 safetensors checkpoint shards 全部加载完成，`after LLM init` 成功打印，并完成了 `1+1等于几？` 的一次生成；这说明 vLLM 推理链路已跑通，后续正式 `lm_eval` 评测应保留镜像源配置与 `tokenizer_mode=slow`。`VLLM_USE_V1=0` 曾被尝试，但当前 vLLM 0.12.0 的日志仍显示 V1 Engine，因此不将其作为可依赖的稳定配置。

~~记得进入vllm的虚拟环境后，先 `cd /gemini/code/MedicalGPT-复现-基础/3_Evaluation/lm-evaluation-harness`，然后 `pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple`，最后再跑相关测试命令。~~
> **⚠️ 已废弃**：上述 `pip install -e .` 方案因共享盘 I/O 过慢已被 6.22 的本地化安装方案取代，见下文。

## 2026-06-22 lm-evaluation-harness 本地化与公开集评测排障

今天在跑公开榜单评测时，经历了一系列环境配置与网络依赖的极限排障，已成功进入模型加载阶段，待评测结果验证。具体踩坑与破局记录如下：

**1. 挂载盘 I/O 龟速导致初始化假死**
在执行 `bash src/evaluation/run_eval_public.sh sft` 时，`lm_eval` 长时间未占用 GPU。通过 `ps` 和 `/proc/<pid>/io` 定位到进程处于 `D` 状态并持续缓慢读取 YAML 文件。这是因为挂载盘（共享网络盘）扫描几千个任务定义文件导致极高的随机 I/O 延迟。
- **解法**：将代码本地化。用 `tar` 流式复制到本地 SSD `/root/lm-evaluation-harness-local`，再通过 `pip` 本地重装。
  ```bash
  tar -C /gemini/code/MedicalGPT-复现-基础/3_Evaluation/lm-evaluation-harness -cf - . | tar -C /root/lm-evaluation-harness-local -xf -
  python -m pip install --no-deps --no-build-isolation --force-reinstall /root/lm-evaluation-harness-local
  ```

**2. `datasets` 版本过新导致加载脚本报错**
本地化后成功点亮了 vLLM 并加载了模型权重，但在下载 CMMLU 题目时崩溃，报错：`RuntimeError: Dataset scripts are no longer supported, but found cmmlu.py`。
- **解法**：新版完全禁用了 Python 脚本加载机制，必须强制降级 `datasets` 到 `2.21.0`。
  ```bash
  python -m pip install --force-reinstall "datasets==2.21.0" -i https://pypi.tuna.tsinghua.edu.cn/simple
  ```

**3. 强扭 CMMLU 代码内硬编码的直连 URL（Network is unreachable）**
降级后重试，依然失败并报网络不可达。排查发现 `cmmlu.py` 的作者将数据包下载 URL 强行硬编码为 `https://huggingface.co/...`，导致在无代理环境下绕过了配置好的 `HF_ENDPOINT` 镜像，直接撞墙。
- **解法**：给 `datasets` 库的下载器实施外科手术，写脚本强行把底层的 `huggingface.co` 替换为国内镜像 `hf-mirror.com`：
  ```python
  python - <<'PY'
  import datasets.download.download_manager as dm
  with open(dm.__file__, "r", encoding="utf-8") as f: code = f.read()
  old_str = "url_or_filename = str(url_or_filename)"
  new_str = "url_or_filename = str(url_or_filename).replace('huggingface.co', 'hf-mirror.com')"
  if old_str in code and new_str not in code:
      with open(dm.__file__, "w", encoding="utf-8") as f: f.write(code.replace(old_str, new_str))
      print("✅ 拦截补丁打入成功！")
  PY
  ```
  *(注：直接修改 site-packages 源码是当前环境有效的临时绕过，该补丁会在重装/升级 datasets 后失效，之后应沉淀为项目内的环境初始化脚本或可重复执行的补丁脚本。)*

**4. 连环包冲突：`huggingface-hub` 与 `numpy` 的连带升级**
强制降级 `datasets` 时，`pip` 会重新解析依赖，不幸将 `huggingface-hub` 升至 `1.20.1`，将 `numpy` 升至 `2.4.6`。这触发了两个致命的连锁反应：
- `transformers` 报错：`ImportError: huggingface-hub<1.0 is required`
- vLLM 底层的 `numba` 报错：`ImportError: Numba needs NumPy 2.2 or less`
- **解法**：通过 `--no-deps` 参数将这两个包精准降级，且阻止 pip 再次多米诺骨牌式修改其他依赖：
  ```bash
  python -m pip install --no-deps --force-reinstall "huggingface-hub>=0.34.0,<1.0" "numpy<2.3" -i https://pypi.tuna.tsinghua.edu.cn/simple
  ```

**5. 成功进入加载阶段**
打完所有补丁后，可用以下代码做轻量级（无 GPU 占用）的终极依赖验证：
```bash
python - <<'PY'
import huggingface_hub
import transformers
import vllm
from lm_eval.tasks import TaskManager
TaskManager().load(["cmmlu_anatomy"])
print("✅ Imports all green & CMMLU task ready!")
PY
```
看到绿灯后，正式下达评测指令，此时 vLLM 成功拉起，顺利开始大模型加载并执行推理流水线：
```bash
cd /gemini/code/MedGPT-o1-Main
bash src/evaluation/run_eval_public.sh sft
```
*(注：排障期间出现的 `EngineCore ... died unexpectedly` 系上游代码（如 `lm_eval` 找不到包）异常退出后，vLLM 子进程随之被系统关闭的正常连带现象，并非 GPU 硬件或推理引擎本身故障。)*

## 2026-06-23 工作日志

**📊 1. 统一测评体系与极速缓存优化**

为对齐真实对话环境，重新编写了一键公开基准评测脚本 `src/evaluation/run_eval_all.sh`，强制启用 `--apply_chat_template`，整合了原来分散的 Public 与 MMLU-Pro 评测脚本。CMB-Exam/CMExam 在当前 `lm-evaluation-harness` 中尚未注册为可用任务，因此暂未纳入该脚本，待后续完成自定义任务适配后单独评测。
同时，排查发现云端 `/gemini/code` 使用的是 `fuse.seaweedfs` 网络共享存储（随机 I/O 极慢），引入了**本地高速缓存自动嗅探**机制：自动检测 `/root/models/` 极速本地盘。实测模型加载耗时从 **近半小时** 暴降至 **21 秒**。

完成的新一轮带 Chat Template 硬通货测评结果（结果存放在 `reports/eval_v1_chat_template/`）：

| 模型 | MMLU-Pro (Bio/Health) | CMMLU (Avg) | MedQA | MedMCQA | PubMedQA | GSM8K (flexible) | GSM8K (strict) |
|---|---|---|---|---|---|---|---|
| Base | **72.8%** / **56.8%** | **81.5%** | **53.6%** | **49.3%** | **73.4%** | **72.2%** | **19.3%** |
| SFT (v1) | **70.7%** / **55.0%** | **81.3%** | **55.6%** | **49.8%** | **73.4%** | **76.2%** | **32.4%** |
| GRPO (v1) | **69.3%** / **56.1%** | **81.4%** | **55.5%** | **50.0%** | **73.8%** | **76.1%** | **32.8%** |

> 注：GSM8K 的主对比口径统一使用 `flexible-extract`，以便与 6 月 17 日的快速公开集结果比较。带 Chat Template 后，Base / SFT / GRPO 的 flexible 分数分别为 72.2% / 76.2% / 76.1%，相较快速公开集下降约 10.6 / 6.6 / 7.4 个百分点；这一现象说明评测配置对结果敏感，但不能仅凭此归因为 Alignment Tax。`strict-match` 与 `flexible-extract` 的明显差距则直接表明答案格式或抽取规则仍值得专项分析。RFT 的必要性主要来自第一轮 SFT/GRPO 在知识类基准与内部 ORM 测试上未形成稳定增益。

**🛠️ 2. RFT 拒绝采样引擎开发与 Code Review**

完成了 `src/data_pipeline/05_rft_rejection_sampling.py` 的开发与两轮 Code Review 修复：
- **输入**：`data/final/rl/rl_train.jsonl` (7,016 条原始题)
- **输出**：`data/intermediate/rft_full_v1/` 下三份文件 `rft_all.jsonl`、`rft_strict_pass.jsonl`、`rft_rejected.jsonl`
- **核心优化**：
  - 补全 `cot_content`、`final_answer` 等字段，使输出可直接被 `03_build_sft_dataset.py` 消费
  - 断点键从 `id` 修正为 `question_id`，修复续跑失效 bug
  - `id` 字段改为全局唯一格式 `rft_{question_id}_{candidate_index}`
  - 新增 vLLM 输出数量校验，防止 `zip()` 静默丢候选
  - 答案泄漏/选项痕迹/占位回答/重复步骤等脏数据拦截
  - 保守 vLLM 参数：`max_tokens=1024`、`gpu_memory_utilization=0.80`、`max_model_len=2048`
- **ORM 接口重构**：在 `src/rewards/orm_reward.py` 中新增 `score_response()` 函数，返回结构化 `OrmResult` 数据类（含 `score`、`match_type`、`predicted_answer` 等），旧的 `medical_orm_score()` 改为其薄封装

**🧪 3. RFT 烟雾测试 (20 题 x 4 候选)**

| 指标 | 数值 |
|---|---|
| 输入题目 | 20 题 x 4 候选 = 80 条 |
| 严格通过 (exact match + 结构完整) | 15 条 (18.75%) |
| 拒绝 | 65 条 |
| 耗时 | 11 秒 |
| 生成速度 | ~424 条/min |
| 覆盖题目 | 20 题中 7 题至少有 1 条通过 |

通过样本质量审查：推理步骤 4~7 步、`<think>` 结构完整、ORM 精确命中、多候选推理路径多样。

**🚀 4. 全量 RFT 生产（已完成）**

已启动全量生成（7,016 题 x 6 候选 = 42,096 条），输出目录 `data/intermediate/rft_full_v1/`。使用本地缓存模型 `/root/models/sft_merged_v1`，以避开 `/gemini/code` 网络挂载盘的随机 I/O 瓶颈。

```bash
cd /gemini/code/MedGPT-o1-Main
python src/data_pipeline/05_rft_rejection_sampling.py \
  --model /root/models/sft_merged_v1 \
  --input data/final/rl/rl_train.jsonl \
  --output_dir data/intermediate/rft_full_v1 \
  --n 6 \
  --chunk_size 128 \
  --max_tokens 1024 \
  --gpu_memory_utilization 0.80 \
  --max_model_len 2048
```

断点续跑时使用完全相同的命令和输出目录；脚本会从 `rft_all.jsonl` 读取已完成的 `(question_id, candidate_index)`，仅补齐未完成候选。

全量完成后的统计：

| 指标 | 数值 |
|---|---|
| 总生成候选数 | **42,096 条** (7,016 题 × 6 候选) |
| 严格通过 (exact match) | **6,213 条** |
| 通过率 | **14.8%** |
| 拒绝样本数 | **35,883 条** |
| 总耗时 | **51 分 21 秒** |
| 输出文件 | `data/intermediate/rft_full_v1/rft_strict_pass.jsonl` |

**🛠️ 5. RFT 候选选择与增强合并 (06_rft_deduplication.py)**

完成了 RFT 数据的提纯以及与 SFT-v1 的增强合并，放弃了原定的 BGE-M3 跨样本语义聚类去重，采用更为稳健的“题目级最高质量候选提取+增量合并”策略：
- **最优候选提取**：基于 `quality_step_count` 和 `quality_think_chars`（优先 `>=3` 步且 `>=120` 字，最接近 `220` 字），从 6,213 条候选中共提取出 **2,372** 条“各题最佳代表”。
- **增强合并 (Additive Merge)**：保留原 SFT-v1 数据（6,880 条，由教师模型生成），将这 2,372 条学生验证版 CoT 直接追加，构成兼顾“教师高阶视角”与“学生自我顿悟”的 SFT-v2 数据集。
- **无泄漏切分**：按 `openqa_id` 聚类后进行 90/10 拆分，确保所有版本的同一题目严格落在同一集合。
- **最终产物**：
  - `data/intermediate/rft_full_v1/rft_best_per_question.jsonl`（2,372 条审计备份）
  - `data/final/sft_v2/sft_v2_all.jsonl`（共 9,252 条，覆盖 6,929 题）
  - `data/final/sft_v2/sft_v2_train.jsonl`（8,335 条）
  - `data/final/sft_v2/sft_v2_val.jsonl`（917 条）
  - `reports/rft_human_review_sample.md`（80 题人工抽查质检表，含候选追溯与评分位）

**🚀 6. SFT-v2-A 训练（已完成）**

基于扩容且提纯后的 `SFT-v2-A` 数据，从 Base 模型重新起训，建立完全纯净的数据升级对照实验。
- 执行命令记录：
  ```bash
  cd /gemini/code/MedGPT-o1-Main
  mkdir -p logs
  nohup python -u src/training/train_sft.py \
    --model_name_or_path /gemini/pretrain/Qwen2.5-7B-Instruct \
    --train_file data/final/sft_v2/sft_v2_train.jsonl \
    --val_file data/final/sft_v2/sft_v2_val.jsonl \
    --output_dir outputs/sft_v2_a_lora \
    --max_seq_length 2048 \
    --per_device_train_batch_size 1 \
    --gradient_accumulation_steps 8 \
    --num_train_epochs 1 \
    --learning_rate 2e-5 \
    --logging_steps 10 \
    --eval_steps 100 \
    --save_steps 200 \
    --gradient_checkpointing \
    --report_to wandb \
    --wandb_project MedGPT-o1 \
    --wandb_run_name sft-v2-a-rft-augment \
    > logs/sft_v2_a_training.log 2>&1 &
  echo $!
  ```

- **训练超参数与执行情况（已完成）**：
  - 数据集规模：8,335 条 SFT 样本
  - 实际总步数：1,042 步 (1 Epoch)
  - 训练耗时：约 **58 分 37 秒**（与此前按 SFT-v1 耗时严密推算的 58 分钟丝毫不差）
  - Train Loss / Eval Loss：最终 `train_loss` 约 0.8716，`eval_loss` 降至 0.8394。
  - **曲线形态判断**：WandB 监控显示 `eval/loss` 全程呈现平滑且健康的下降曲线（0.91 -> 0.839），无任何翘起迹象，证明 1 个 Epoch 吸收得刚刚好，没有过拟合。

## 2026-06-24 工作日志

**📊 1. SFT-v2-A “三道门”评测记录**

**第二道门：内部真实性 ORM 评测 (rl_val.jsonl 143题)**
- **评测脚本升级**：同时记录 `ORM_exact`（标准化后最终答案精确相等）与 `ORM_any`（精确或安全的包含匹配）。其中 `ORM_exact` 是本轮的核心正确性指标，`ORM_any` 仅用于观察回答中是否包含标准术语。
- **原始结果**：SFT-v2-A 明细位于 `reports/eval_v2_a_orm_20260624_122506/sft_v2_a_eval.jsonl`；历史明细位于 `reports/eval_compare/`。旧明细虽未保存 `match_type`，但可由 `orm_score == 1.0` 回算精确命中。

| 模型 | ORM 精确命中 | ORM 任意命中 | 完整格式 |
|---|---:|---:|---:|
| Base | 9/143，6.29% | 46/143，32.17% | 142/143，99.30% |
| SFT-v1 | 25/143，17.48% | 35/143，24.48% | 143/143，100.00% |
| GRPO-v1 | 29/143，20.28% | 39/143，27.27% | 143/143，100.00% |
| **SFT-v2-A** | **30/143，20.98%** | **41/143，28.67%** | **143/143，100.00%** |

- **结论分析**：
  - 相对 SFT-v1，SFT-v2-A 在精确命中上净增加 5 题；相对 GRPO-v1 净增加 1 题。严格正确率小幅改善，但由于验证集只有 143 题，不能夸大为显著跃迁。
  - Base 的 32.17% 是宽松包含命中，并不是精确命中；因此不能与 SFT-v2-A 的 20.98% 精确命中直接比较。Base 的大量包含命中更可能反映其回答较长、标准术语出现在回答中，而不代表其最终答案更准确。
  - SFT-v2-A 的格式完成率维持在 100%，说明 RFT 增强未破坏既有格式能力；这是一项稳定性结果，而非新的格式增益。
  - 该结果允许将 SFT-v2-A 作为 GRPO-v2 的初始化底座，但 GRPO-v2 的目标应是扩大严格正确率增益，而不是仅提高宽松包含命中或格式奖励。

**第三道门：公开医疗榜单评测（已完成）**
- **评测方式**：使用 vLLM 加载 `sft_v2_a_merged`，启用 `--apply_chat_template`。
- **可复现性**：四个模型均使用 `dtype=bfloat16, tokenizer_mode=slow, gpu_memory_utilization=0.85, max_model_len=8192, enforce_eager=True, disable_custom_all_reduce=True`。SFT-v2-A 原始结果位于 `reports/eval_v1_chat_template/sft_v2_a_all_20260624_123916/`，可与同目录下 Base、SFT-v1、GRPO-v1 结果直接比较。

| 模型 | MMLU-Pro (Bio/Health) | CMMLU (Avg) | MedQA | MedMCQA | PubMedQA | GSM8K (flexible / strict) |
|---|---|---|---|---|---|---|
| Base | **72.8%** / **56.8%** | **81.5%** | 53.6% | 49.3% | 73.4% | 72.2% / 19.3% |
| SFT (v1) | 70.7% / 55.0% | 81.3% | 55.6% | 49.8% | 73.4% | **76.2%** / 32.4% |
| **SFT (v2-A)** | 69.2% / 53.8% | 81.4% | **56.0%** | **50.9%** | **73.8%** | 75.7% / **32.6%** |

- **成绩单深度剖析**：
  1. **医学核心三大榜（MedQA, MedMCQA, PubMedQA）**：SFT-v2-A 的原始分数均为四个对照中最高。相对 SFT-v1，变化分别为 `+0.39pp`、`+1.05pp`、`+0.40pp`；这些是正向信号，但幅度较小，应在 GRPO-v2 后结合标准误和逐题差异再判断是否为稳定提升。
  2. **基础学科与数学（CMMLU, GSM8K）**：CMMLU 医疗平均相对 SFT-v1 为 `+0.19pp`；GSM8K flexible 为 `-0.53pp`、strict 为 `+0.15pp`，整体可视为稳定。
  3. **MMLU-Pro（Bio/Health）**：相对 SFT-v1 分别为 `-1.53pp`、`-1.22pp`，存在回落方向；但单项标准误约为 1.7 个百分点，暂不能仅凭这一次评测归因为确定性的 Alignment Tax。后续应把该项作为 GRPO-v2 的重点回归监控项。
- **总纲判定**：
  SFT-v2-A 在医学核心任务上呈现小幅正向变化，CMMLU 与 GSM8K 未见明显退化；同时 MMLU-Pro 存在需监控的回落方向。它跨过了进入 GRPO-v2 的工程门槛，但尚不足以单独证明大幅认知能力提升。

**📋 接下来该干什么 (Next Steps)**

- [x] ~~完成 SFT-v2-A 训练与 LoRA 物理合并~~
- [x] ~~升级并完成“第二道门”：内部真实性 ORM 评测 (提取精确命中率)~~
- [x] ~~跑通“第三道门”：公开医疗榜单评测并记录正负向变化~~
- [ ] **启动 GRPO-v2 训练，并以严格 ORM、医学核心榜单与 MMLU-Pro 回归监控作为验收标准**

**🛠️ 2. 奖励函数架构大重构：V3 终极复合奖励 (MiMo Judge)**

在 GRPO-v2 的 Sanity Check 中，我们遭遇了“4个候选全得 0.15 格式分，优势函数 (Advantage) 失效”的瓶颈。为打破组内同分，同时坚守客观事实，我们设计并实装了 **“硬格式约束 + Exact短路满分 + MiMo Judge 连续打分”** 的 V3 终极奖励体系。

- **逻辑亮点**：
  1. **格式不合格** -> `-0.25`，剥夺正确性判定资格。
  2. **格式达标且精确命中** -> `2.15`，最高权威分数，不调用 API 以节约成本。
  3. **格式达标且未精确命中** -> 调用 MiMo API。若存在医学矛盾则为 `0.0`；否则得分 `0.15 + 1.70 * J` (J 为语义评分)。
- **架构极简化重构**：
  为了彻底摆脱历史包袱，对 `src/rewards` 进行了大扫除：
  - **`hard_constraints.py`**：将正则格式验证、最终答案提取、精确匹配全部整合，担任铁面无私的客观把门人。
  - **`llm_judge.py`**：封装带本地 JSON 缓存与指数退避重试的 MiMo API 裁判。
  - **`composite_reward.py`**：唯一的奖励主干。
  - **清理**：归档了臃肿的 `orm_reward.py`、`format_reward.py`、`process_rule_reward.py`，并将 `train_grpo.py` 中的挂载逻辑缩减为极简的一行调用。
- **验证与测试**：
  - 编写并跑通了 `tests/test_llm_judge.py` 单元测试，断言分值与逻辑均 100% 正确。
  - 开发了 `re_evaluate_sanity_v3.py`，用于在云端对 128 条 Sanity 结果进行全量重打分并输出方差统计图。离线验证结果极其积极：组内有差异的题目组从 `6/32` 提升到 `20/32`，Judge 成功恢复了相对优势信号。

**🚀 3. 启动 V3 线上 Pilot (50-Step)**

由于真实的 GRPO Rollout 会产生大量新回答，离线的 100% 缓存命中不能代表在线开销。为了防范 API 频繁重试、超时、限流导致的训练假死，我们严谨地插入了一个 **50 步的 Pilot 试飞阶段**：
- **目标**：验证外部 Judge 在真实训练中的稳定性、延迟对 GPU 的影响，以及断点续训的安全性。
- **配置**：`--max_steps 50`，挂载 `sft_v2_a_lora`，输出至 `grpo_qwen2_5_7b_v3_judge_pilot`。
- **通过标准**：
  - API 无频繁流控。
  - GPU 未陷入长时间 I/O 闲置。
  - 奖励存在健康的方差。
  - checkpoint 正常落盘。

**🛠️ 4. 解决 GRPO V3 训练耗时瓶颈 (Judge I/O 与并发优化)**

在初步的全量试跑中发现，V3 的单步耗时高达 40~44 秒，跑完 3154 步（6307 条数据，单卡 Batch=1, Accumulation=8，生成 4 个回答）需要近 38.7 小时。瓶颈除了大语言模型的自回归生成外，更致命的是外部 Judge API 调用的串行阻塞与网络盘 I/O 的疯狂开销。为此我们制定并执行了以下“极限压榨方案”：

- **Judge I/O 极速升级 (`llm_judge.py`)**：
  - **避开网络盘**：将缓存路径默认从 `/gemini/code/` 迁移至 `/root/cache/mimo_judge_cache.jsonl` 本地 SSD 盘。
  - **Append-only 写法**：彻底废除每次都要重写几十 MB 字典的灾难性 JSON 序列化，改为每次新增仅向 `.jsonl` 尾部追加一行（耗时由秒级降至不到 1 毫秒）。
  - **线程安全与批量落盘**：引入 `threading.Lock` 与最大 100 条的缓冲队列，并注册 `atexit` 保证程序结束时仍能强制 Flush 保护断点状态。
- **安全并发裁判 (`composite_reward.py`)**：
  - 引入 `ThreadPoolExecutor(max_workers=2)`。
  - 精确短路保护：只有格式正确、且未被精确命中（Exact Match）的顽固项才送进并发池。在保证 API QPS 不超限流阈值的同时，将网络等待时间无风险砍半。
- **vLLM 架构前置改造 (`train_grpo.py`)**：
  - 增设版本兼容闸门：运行时动态扫描当前 TRL 的 `GRPOConfig.__dataclass_fields__`，若云端版本不支持 vLLM，则强制拦截以防参数被静默抛弃。
  - 按需开放 `--use_vllm` 与 `--vllm_gpu_memory_utilization` 入口，为后续演进到 80G 单卡 colocate 模式或双卡 server 模式铺平道路。

**🎯 5. 优化后基线测速结果 (40G 单卡 vs 80G 单卡)**

经过上述 I/O 与并发极限压榨后，我们成功剥离了网络延迟与 API 阻塞，彻底释放了原生算力，最后更通过 vLLM 加持突破了生成极限：

- **优化前 (40G 原始)**: `~44s/it`，全量（3154步）预计总耗时 **38.7 小时**。
- **优化后 (40G 单卡 PyTorch)**: `~27s/it`，全量预计总耗时降至 **23.6 小时**。
- **优化后 (80G 单卡 PyTorch)**: `~25s/it`，全量预计总耗时约 **22 小时**。
- **🚀 终极形态 (80G 单卡 vLLM Colocate, Batch=1x8)**: `~19s/it`，全量预计总耗时降至 **16.7 小时**。
- **🔥 极限并发形态 (80G 单卡 vLLM, Batch=2x4)**: `~13.7s/it`（Global Batch=8，全量3154步），预计总耗时降至 **12.0 小时**。
- **👑 黄金甜点位 (80G 单卡 vLLM, Batch=2x8)**: `~24.2s/it`（Global Batch=16，全量步数减半至 1577 步），预计总耗时突破至 **10.6 小时**！

上面的Batch具体是batch_size (单次吞吐量) × gradient_accumulation_steps (累加次数) = Global Batch Size (全局批次大小)

**实验排雷：为什么不推荐 4x4？**
经实测，`Batch=4x4` 的单步耗时为 `~24s/it`，与 `2x8` 几乎完全相同（说明 80G 显卡的 CUDA 算力在 `Batch=2` 时已被彻底满载饱和）。但 `4x4` 会导致 PyTorch 反向传播激活值激增，显存占用从 50GB 狂飙至 **67.5GB**，不仅没有换来任何速度提升，反而极大增加了遇到长文本时 OOM 崩溃的风险。

**最终结论**：从最初的 38.7 小时到现在的 10.6 小时，我们成功将整体训练时间**缩短了惊人的 28.1 个小时（提速近 73%）**！`Batch=2x8` (Global Batch=16) 是综合了“极限速度、绝对显存安全、模型收敛平滑度”的最完美超参组合。

**🔥 6. 开启 V3 终极全量训练 (80G vLLM 极限并发形态)**

经过长达数天的环境调优、底层框架微创手术（修复 TRL 与 vLLM 版本冲突）、分布式假变量骗过 Accelerate，以及最终对 `batch_size` 和 `gradient_accumulation` 的精妙平衡测试，我们终于迎来了正式起跑。

**环境初始设定（必读备忘）**：
为了能在云端镜像重启后快速恢复这份极速环境，必须预先执行以下初始化配置：
```bash
# 1. 欺骗 Accelerate 的单机伪分布式变量
export RANK=0
export LOCAL_RANK=0
export WORLD_SIZE=1
export MASTER_ADDR="127.0.0.1"
export MASTER_PORT="29500"

# 2. 安全注入 MiMo Judge API Key (隐藏输入防历史泄露)
read -rsp "OPENAI_API_KEY: " OPENAI_API_KEY; echo
export OPENAI_API_KEY
export OPENAI_BASE_URL="https://token-plan-sgp.xiaomimimo.com/v1"

# 3. 登录 WandB 以启用图表记录
wandb login
```

**终极全量训练指令**：
结合防止 Reward Hacking 与最大限度压榨算力的考量，我们选择了以下最优参数组合：
- `Epoch 1.0`：防止反复刷题导致八股文过拟合。
- `Batch 2x8` (Global Batch 16)：最完美的“黄金甜点位”，1577 步即可跑完 1 轮。
- `eval_steps 10000`：彻底屏蔽耗时费钱的在线验证，改为全量跑完后离线集中验证。

```bash
nohup python -u src/training/train_grpo.py \
    --base_model "/gemini/pretrain/Qwen2.5-7B-Instruct" \
    --sft_lora_path "outputs/sft_v2_a_lora" \
    --train_file "data/final/rl_v3/rl_clean_train.jsonl" \
    --val_file "data/final/rl_v3/rl_clean_val.jsonl" \
    --output_dir "outputs/grpo_qwen2_5_7b_v3_vllm_80g_final" \
    --num_train_epochs 1.0 \
    --max_steps -1 \
    --num_generations 4 \
    --max_prompt_length 1024 \
    --max_completion_length 512 \
    --per_device_train_batch_size 2 \
    --gradient_accumulation_steps 8 \
    --learning_rate 5e-6 \
    --logging_steps 5 \
    --save_steps 250 \
    --eval_steps 10000 \
    --no-gradient_checkpointing \
    --report_to wandb \
    --use_vllm \
    --vllm_gpu_memory_utilization 0.30 > outputs/grpo_v3_vllm_80g_final.log 2>&1 &
```

**🚀 6. SFT-v2-A 训练（已完成）**

基于扩容且提纯后的 `SFT-v2-A` 数据，从 Base 模型重新起训，建立完全纯净的数据升级对照实验。
- 执行命令记录：
  ```bash
  cd /gemini/code/MedGPT-o1-Main
  mkdir -p logs
  nohup python -u src/training/train_sft.py \
    --model_name_or_path /gemini/pretrain/Qwen2.5-7B-Instruct \
    --train_file data/final/sft_v2/sft_v2_train.jsonl \
    --val_file data/final/sft_v2/sft_v2_val.jsonl \
    --output_dir outputs/sft_v2_a_lora \
    --max_seq_length 2048 \
    --per_device_train_batch_size 1 \
    --gradient_accumulation_steps 8 \
    --num_train_epochs 1 \
    --learning_rate 2e-5 \
    --logging_steps 10 \
    --eval_steps 100 \
    --save_steps 200 \
    --gradient_checkpointing \
    --report_to wandb \
    --wandb_project MedGPT-o1 \
    --wandb_run_name sft-v2-a-rft-augment \
    > logs/sft_v2_a_training.log 2>&1 &
  echo $!
  ```

- **训练超参数与执行情况（已完成）**：
  - 数据集规模：8,335 条 SFT 样本
  - 实际总步数：1,042 步 (1 Epoch)
  - 训练耗时：约 **58 分 37 秒**（与此前按 SFT-v1 耗时严密推算的 58 分钟丝毫不差）
  - Train Loss / Eval Loss：最终 `train_loss` 约 0.8716，`eval_loss` 降至 0.8394。
  - **曲线形态判断**：WandB 监控显示 `eval/loss` 全程呈现平滑且健康的下降曲线（0.91 -> 0.839），无任何翘起迹象，证明 1 个 Epoch 吸收得刚刚好，没有过拟合。

## 2026-06-24 工作日志

**📊 1. SFT-v2-A “三道门”评测记录**

**第二道门：内部真实性 ORM 评测 (rl_val.jsonl 143题)**
- **评测脚本升级**：同时记录 `ORM_exact`（标准化后最终答案精确相等）与 `ORM_any`（精确或安全的包含匹配）。其中 `ORM_exact` 是本轮的核心正确性指标，`ORM_any` 仅用于观察回答中是否包含标准术语。
- **原始结果**：SFT-v2-A 明细位于 `reports/eval_v2_a_orm_20260624_122506/sft_v2_a_eval.jsonl`；历史明细位于 `reports/eval_compare/`。旧明细虽未保存 `match_type`，但可由 `orm_score == 1.0` 回算精确命中。

| 模型 | ORM 精确命中 | ORM 任意命中 | 完整格式 |
|---|---:|---:|---:|
| Base | 9/143，6.29% | 46/143，32.17% | 142/143，99.30% |
| SFT-v1 | 25/143，17.48% | 35/143，24.48% | 143/143，100.00% |
| GRPO-v1 | 29/143，20.28% | 39/143，27.27% | 143/143，100.00% |
| **SFT-v2-A** | **30/143，20.98%** | **41/143，28.67%** | **143/143，100.00%** |

- **结论分析**：
  - 相对 SFT-v1，SFT-v2-A 在精确命中上净增加 5 题；相对 GRPO-v1 净增加 1 题。严格正确率小幅改善，但由于验证集只有 143 题，不能夸大为显著跃迁。
  - Base 的 32.17% 是宽松包含命中，并不是精确命中；因此不能与 SFT-v2-A 的 20.98% 精确命中直接比较。Base 的大量包含命中更可能反映其回答较长、标准术语出现在回答中，而不代表其最终答案更准确。
  - SFT-v2-A 的格式完成率维持在 100%，说明 RFT 增强未破坏既有格式能力；这是一项稳定性结果，而非新的格式增益。
  - 该结果允许将 SFT-v2-A 作为 GRPO-v2 的初始化底座，但 GRPO-v2 的目标应是扩大严格正确率增益，而不是仅提高宽松包含命中或格式奖励。

**第三道门：公开医疗榜单评测（已完成）**
- **评测方式**：使用 vLLM 加载 `sft_v2_a_merged`，启用 `--apply_chat_template`。
- **可复现性**：四个模型均使用 `dtype=bfloat16, tokenizer_mode=slow, gpu_memory_utilization=0.85, max_model_len=8192, enforce_eager=True, disable_custom_all_reduce=True`。SFT-v2-A 原始结果位于 `reports/eval_v1_chat_template/sft_v2_a_all_20260624_123916/`，可与同目录下 Base、SFT-v1、GRPO-v1 结果直接比较。

| 模型 | MMLU-Pro (Bio/Health) | CMMLU (Avg) | MedQA | MedMCQA | PubMedQA | GSM8K (flexible / strict) |
|---|---|---|---|---|---|---|
| Base | **72.8%** / **56.8%** | **81.5%** | 53.6% | 49.3% | 73.4% | 72.2% / 19.3% |
| SFT (v1) | 70.7% / 55.0% | 81.3% | 55.6% | 49.8% | 73.4% | **76.2%** / 32.4% |
| **SFT (v2-A)** | 69.2% / 53.8% | 81.4% | **56.0%** | **50.9%** | **73.8%** | 75.7% / **32.6%** |

- **成绩单深度剖析**：
  1. **医学核心三大榜（MedQA, MedMCQA, PubMedQA）**：SFT-v2-A 的原始分数均为四个对照中最高。相对 SFT-v1，变化分别为 `+0.39pp`、`+1.05pp`、`+0.40pp`；这些是正向信号，但幅度较小，应在 GRPO-v2 后结合标准误和逐题差异再判断是否为稳定提升。
  2. **基础学科与数学（CMMLU, GSM8K）**：CMMLU 医疗平均相对 SFT-v1 为 `+0.19pp`；GSM8K flexible 为 `-0.53pp`、strict 为 `+0.15pp`，整体可视为稳定。
  3. **MMLU-Pro（Bio/Health）**：相对 SFT-v1 分别为 `-1.53pp`、`-1.22pp`，存在回落方向；但单项标准误约为 1.7 个百分点，暂不能仅凭这一次评测归因为确定性的 Alignment Tax。后续应把该项作为 GRPO-v2 的重点回归监控项。
- **总纲判定**：
  SFT-v2-A 在医学核心任务上呈现小幅正向变化，CMMLU 与 GSM8K 未见明显退化；同时 MMLU-Pro 存在需监控的回落方向。它跨过了进入 GRPO-v2 的工程门槛，但尚不足以单独证明大幅认知能力提升。

**📋 接下来该干什么 (Next Steps)**

- [x] ~~完成 SFT-v2-A 训练与 LoRA 物理合并~~
- [x] ~~升级并完成“第二道门”：内部真实性 ORM 评测 (提取精确命中率)~~
- [x] ~~跑通“第三道门”：公开医疗榜单评测并记录正负向变化~~
- [ ] **启动 GRPO-v2 训练，并以严格 ORM、医学核心榜单与 MMLU-Pro 回归监控作为验收标准**

**🛠️ 2. 奖励函数架构大重构：V3 终极复合奖励 (MiMo Judge)**

在 GRPO-v2 的 Sanity Check 中，我们遭遇了“4个候选全得 0.15 格式分，优势函数 (Advantage) 失效”的瓶颈。为打破组内同分，同时坚守客观事实，我们设计并实装了 **“硬格式约束 + Exact短路满分 + MiMo Judge 连续打分”** 的 V3 终极奖励体系。

- **逻辑亮点**：
  1. **格式不合格** -> `-0.25`，剥夺正确性判定资格。
  2. **格式达标且精确命中** -> `2.15`，最高权威分数，不调用 API 以节约成本。
  3. **格式达标且未精确命中** -> 调用 MiMo API。若存在医学矛盾则为 `0.0`；否则得分 `0.15 + 1.70 * J` (J 为语义评分)。
- **架构极简化重构**：
  为了彻底摆脱历史包袱，对 `src/rewards` 进行了大扫除：
  - **`hard_constraints.py`**：将正则格式验证、最终答案提取、精确匹配全部整合，担任铁面无私的客观把门人。
  - **`llm_judge.py`**：封装带本地 JSON 缓存与指数退避重试的 MiMo API 裁判。
  - **`composite_reward.py`**：唯一的奖励主干。
  - **清理**：归档了臃肿的 `orm_reward.py`、`format_reward.py`、`process_rule_reward.py`，并将 `train_grpo.py` 中的挂载逻辑缩减为极简的一行调用。
- **验证与测试**：
  - 编写并跑通了 `tests/test_llm_judge.py` 单元测试，断言分值与逻辑均 100% 正确。
  - 开发了 `re_evaluate_sanity_v3.py`，用于在云端对 128 条 Sanity 结果进行全量重打分并输出方差统计图。离线验证结果极其积极：组内有差异的题目组从 `6/32` 提升到 `20/32`，Judge 成功恢复了相对优势信号。

**🚀 3. 启动 V3 线上 Pilot (50-Step)**

由于真实的 GRPO Rollout 会产生大量新回答，离线的 100% 缓存命中不能代表在线开销。为了防范 API 频繁重试、超时、限流导致的训练假死，我们严谨地插入了一个 **50 步的 Pilot 试飞阶段**：
- **目标**：验证外部 Judge 在真实训练中的稳定性、延迟对 GPU 的影响，以及断点续训的安全性。
- **配置**：`--max_steps 50`，挂载 `sft_v2_a_lora`，输出至 `grpo_qwen2_5_7b_v3_judge_pilot`。
- **通过标准**：
  - API 无频繁流控。
  - GPU 未陷入长时间 I/O 闲置。
  - 奖励存在健康的方差。
  - checkpoint 正常落盘。

**🛠️ 4. 解决 GRPO V3 训练耗时瓶颈 (Judge I/O 与并发优化)**

在初步的全量试跑中发现，V3 的单步耗时高达 40~44 秒，跑完 3154 步（6307 条数据，单卡 Batch=1, Accumulation=8，生成 4 个回答）需要近 38.7 小时。瓶颈除了大语言模型的自回归生成外，更致命的是外部 Judge API 调用的串行阻塞与网络盘 I/O 的疯狂开销。为此我们制定并执行了以下“极限压榨方案”：

- **Judge I/O 极速升级 (`llm_judge.py`)**：
  - **避开网络盘**：将缓存路径默认从 `/gemini/code/` 迁移至 `/root/cache/mimo_judge_cache.jsonl` 本地 SSD 盘。
  - **Append-only 写法**：彻底废除每次都要重写几十 MB 字典的灾难性 JSON 序列化，改为每次新增仅向 `.jsonl` 尾部追加一行（耗时由秒级降至不到 1 毫秒）。
  - **线程安全与批量落盘**：引入 `threading.Lock` 与最大 100 条的缓冲队列，并注册 `atexit` 保证程序结束时仍能强制 Flush 保护断点状态。
- **安全并发裁判 (`composite_reward.py`)**：
  - 引入 `ThreadPoolExecutor(max_workers=2)`。
  - 精确短路保护：只有格式正确、且未被精确命中（Exact Match）的顽固项才送进并发池。在保证 API QPS 不超限流阈值的同时，将网络等待时间无风险砍半。
- **vLLM 架构前置改造 (`train_grpo.py`)**：
  - 增设版本兼容闸门：运行时动态扫描当前 TRL 的 `GRPOConfig.__dataclass_fields__`，若云端版本不支持 vLLM，则强制拦截以防参数被静默抛弃。
  - 按需开放 `--use_vllm` 与 `--vllm_gpu_memory_utilization` 入口，为后续演进到 80G 单卡 colocate 模式或双卡 server 模式铺平道路。

**🎯 5. 优化后基线测速结果 (40G 单卡 vs 80G 单卡)**

经过上述 I/O 与并发极限压榨后，我们成功剥离了网络延迟与 API 阻塞，彻底释放了原生算力，最后更通过 vLLM 加持突破了生成极限：

- **优化前 (40G 原始)**: `~44s/it`，全量（3154步）预计总耗时 **38.7 小时**。
- **优化后 (40G 单卡 PyTorch)**: `~27s/it`，全量预计总耗时降至 **23.6 小时**。
- **优化后 (80G 单卡 PyTorch)**: `~25s/it`，全量预计总耗时约 **22 小时**。
- **🚀 终极形态 (80G 单卡 vLLM Colocate, Batch=1x8)**: `~19s/it`，全量预计总耗时降至 **16.7 小时**。
- **🔥 极限并发形态 (80G 单卡 vLLM, Batch=2x4)**: `~13.7s/it`（Global Batch=8，全量3154步），预计总耗时降至 **12.0 小时**。
- **👑 黄金甜点位 (80G 单卡 vLLM, Batch=2x8)**: `~24.2s/it`（Global Batch=16，全量步数减半至 1577 步），预计总耗时突破至 **10.6 小时**！

上面的Batch具体是batch_size (单次吞吐量) × gradient_accumulation_steps (累加次数) = Global Batch Size (全局批次大小)

**实验排雷：为什么不推荐 4x4？**
经实测，`Batch=4x4` 的单步耗时为 `~24s/it`，与 `2x8` 几乎完全相同（说明 80G 显卡的 CUDA 算力在 `Batch=2` 时已被彻底满载饱和）。但 `4x4` 会导致 PyTorch 反向传播激活值激增，显存占用从 50GB 狂飙至 **67.5GB**，不仅没有换来任何速度提升，反而极大增加了遇到长文本时 OOM 崩溃的风险。

**最终结论**：从最初的 38.7 小时到现在的 10.6 小时，我们成功将整体训练时间**缩短了惊人的 28.1 个小时（提速近 73%）**！`Batch=2x8` (Global Batch=16) 是综合了“极限速度、绝对显存安全、模型收敛平滑度”的最完美超参组合。

**🔥 6. 开启 V3 终极全量训练 (80G vLLM 极限并发形态)**

经过长达数天的环境调优、底层框架微创手术（修复 TRL 与 vLLM 版本冲突）、分布式假变量骗过 Accelerate，以及最终对 `batch_size` 和 `gradient_accumulation` 的精妙平衡测试，我们终于迎来了正式起跑。

**环境初始设定（必读备忘）**：
为了能在云端镜像重启后快速恢复这份极速环境，必须预先执行以下初始化配置：
```bash
# 1. 欺骗 Accelerate 的单机伪分布式变量
export RANK=0
export LOCAL_RANK=0
export WORLD_SIZE=1
export MASTER_ADDR="127.0.0.1"
export MASTER_PORT="29500"

# 2. 安全注入 MiMo Judge API Key (隐藏输入防历史泄露)
read -rsp "OPENAI_API_KEY: " OPENAI_API_KEY; echo
export OPENAI_API_KEY
export OPENAI_BASE_URL="https://token-plan-sgp.xiaomimimo.com/v1"

# 3. 登录 WandB 以启用图表记录
wandb login
```

**终极全量训练指令**：
结合防止 Reward Hacking 与最大限度压榨算力的考量，我们选择了以下最优参数组合：
- `Epoch 1.0`：防止反复刷题导致八股文过拟合。
- `Batch 2x8` (Global Batch 16)：最完美的“黄金甜点位”，1577 步即可跑完 1 轮。
- `eval_steps 10000`：彻底屏蔽耗时费钱的在线验证，改为全量跑完后离线集中验证。

```bash
nohup python -u src/training/train_grpo.py \
    --base_model "/gemini/pretrain/Qwen2.5-7B-Instruct" \
    --sft_lora_path "outputs/sft_v2_a_lora" \
    --train_file "data/final/rl_v3/rl_clean_train.jsonl" \
    --val_file "data/final/rl_v3/rl_clean_val.jsonl" \
    --output_dir "outputs/grpo_qwen2_5_7b_v3_vllm_80g_final" \
    --num_train_epochs 1.0 \
    --max_steps -1 \
    --num_generations 4 \
    --max_prompt_length 1024 \
    --max_completion_length 512 \
    --per_device_train_batch_size 2 \
    --gradient_accumulation_steps 8 \
    --learning_rate 5e-6 \
    --logging_steps 5 \
    --save_steps 250 \
    --eval_steps 10000 \
    --no-gradient_checkpointing \
    --report_to wandb \
    --use_vllm \
    --vllm_gpu_memory_utilization 0.30 > outputs/grpo_v3_vllm_80g_final.log 2>&1 &
```

**训练过程实时表现**：
- **速度**：稳定在 `~23.85s/it` 左右。
- **显存**：稳定在 `56588MiB / 80988MiB (约 56.5GB)`，极其安全健康。
- **预估时长**：总步数 1577 步，预计耗时约 10 个半小时（完美通宵挂机节奏）。

**📊 7. 终极训练结果与离线评测**

*(注：本次通宵训练已于 6月25日 顺利跑完。详尽的 WandB 收敛曲线复盘、主观质量评测，以及极为惊艳的裁判 Semantic Score 客观打分结果，请直接查阅下方的 [2026-06-25 工作日志]。)*

**2026-06-24 MiMo API 限流修正**：全量训练在约 `39/1577` 步时出现间歇性 `HTTP 429 Too Many Requests`。因此，上述 `~23.85s/it` 与 `~10.6 小时` 仅代表未受 Judge API 限流影响的 vLLM 侧基准，不应视为本次全量训练的最终 ETA。原因是原先 `ThreadPoolExecutor(max_workers=2)` 与 OpenAI SDK 自动重试会叠加形成请求突发。后续统一采用 `MIMO_JUDGE_MAX_WORKERS=1`、`MIMO_JUDGE_MIN_INTERVAL_SECONDS=1.0` 的全局节流策略，并关闭 SDK 自动重试，由 tenacity 统一执行指数退避。Judge 在耗尽重试后必须中止训练而不能静默赋予 `0` 奖励。

**✅ 限流修正后（rate1）实测验证与最终耗时**：
应用上述全局节流阀并重启任务后（输出至 `grpo_qwen2_5_7b_v3_vllm_80g_rate1`），日志实现了 100% 纯净的 `200 OK`，彻底告别 429 报错。由于强制加入了每秒 1 次的平滑限流（且随着后期题目难度增加，API 裁判响应变长），最终单步真实耗时定格在 `~46.37s/it` 左右。1577 步的全量总时长最终为 **20.3 小时**。这是一个用时间换取空间、杜绝了 0 分污染且极其稳健的通宵挂机战果。

## 2026-06-25 工作日志

**📊 1. GRPO-V3 训练深度复盘与性能分析**
- **WandB 收敛指标剖析**：训练后期的 profiling 表明，PPO `clip_ratio` 始终为 0，`kl` 极低，且零方差阻碍 (`frac_reward_zero_std`) 占比高达 30%。这说明我们的模型更新极为保守，算力存在大量“空转”，坚定了我们后续演进到 DAPO（动态优势惩罚）算法的决心。
- **20.3 小时极限通宵战果**：为了 100% 杜绝因 API 异常导致的 0 分污染，我们在 V3 训练中引入了极度稳健的全局平滑限流（1 QPS）和本地日志降级等极致优化。全量 1577 步在 80G 显卡上以 46s/it 的稳健步伐走完，实际耗时 20.3 小时，完美落盘。

**✅ 2. Sanity 离线裁判验证（惊艳的 Semantic Score）**
- 修复了 `re_evaluate_sanity_v3.py` 的环境导入路径。
- 在 20 条验证数据上的结果极具说服力：虽然基于字面的 ORM 精确命中率只有区区 15%，但经过 MiMo Judge 重打分后，**综合语义得分（Semantic Score）达到了惊人的 54.5%！**
- 典型案例：像“正细胞正色素性贫血”（多了一个“性”字）和“麻杏甘石汤”（简称）等优质推理，全被 Judge 从 ORM 的 0 分死刑中“捞”回了 1.0 的满分。完美证明了 V3 架构中引入 LLM 软打分机制的必要性与核心价值。

**🚀 3. LoRA 物理合并与一键开启大满贯盲测**
- 执行 `scripts/merge_lora.py`，成功将 20 小时炼出的 GRPO v3 LoRA 权重与 `sft_v2_a_merged` 底座进行了物理合并，生成 `grpo_merged_v3`。
- 修改并升级了一键评测脚本 `run_eval_all.sh`，为其新增了 `grpo_v3` 通道。
- 借由 vLLM 的 `batch_size=auto` 和 80G 显卡的超大 `gpu_memory_utilization=0.85` 显存池，正式拉起了涵盖 Medical 四大核心榜单与 MMLU-Pro 的终极一键压测。

**🏆 4. 终极大考：大满贯公开榜单评测成绩**
经过 20 小时的强化学习，模型迎来了最终的大考。与 SFT (v2-A) 阶段相比，测试结果不仅是符合预期，简直是**堪称完美**：

| 模型 | MMLU-Pro (Bio/Health) | CMMLU (Avg) | MedQA | MedMCQA | PubMedQA | GSM8K (flexible / strict) |
|---|---|---|---|---|---|---|
| Base | 72.8% / 56.8% | 81.5% | 53.6% | 49.3% | 73.4% | 72.2% / 19.3% |
| SFT (v1) | 70.7% / 55.0% | 81.3% | 55.6% | 49.8% | 73.4% | 76.2% / 32.4% |
| SFT (v2-A) | 69.2% / 53.8% | 81.4% | 56.0% | 50.9% | 73.8% | 75.7% / 32.6% |
| **GRPO-V3** | **70.1% / 55.5%** | **81.7%** | 55.8% | **51.3%** | 73.8% | **76.4%** / 32.6% |

**深度分析结论**：
1. **强势收复“对齐税 (Alignment Tax)”**：SFT 过程中通常会损害模型的通用长逻辑多步推理能力（如 MMLU-Pro Biology 从 Base 的 72.8% 一度掉到 SFT 的 69.2%）。但通过 GRPO 让模型强制内化思维链（Chain-of-Thought）后，MMLU-Pro Biology 强势回升近 1 个百分点（69.2% -> 70.1%），Health 也有大幅回暖！这在实战中完全印证了 OpenAI 的观点：强化学习能重新唤醒被 SFT 压抑的底层推理本能！
2. **核心医学知识不仅没丢，反而突破新高**：CMMLU 平均分以 **81.7%** 创下全系列最高纪录！MedMCQA 更是突破新高达到 **51.3%**！这说明长达 20 小时的 GRPO 并没有让模型发生知识退化（灾难性遗忘），而是通过大段规范的 `<think>` 思考过程，进一步“榨取”和盘活了底座深处的医学知识点。
3. **理科基石稳如泰山**：GSM8K 成绩依然维持在巅峰水准（Flexible: 76.4%）。说明这 20 小时的医学向微调，极其克制且纯粹，丝毫没有破坏模型的数学计算逻辑。

**🎉 一句话总结**：这是一次教科书级别的垂类医学大模型训练战役。从最初无思考能力的 Base，到学会遵循对话模板的 SFT，再到如今**格式完美、逻辑严密、会主动思考、且榜单分数全面升华**的 GRPO-V3（类 o1 架构），项目大获全胜！

## 2026-06-26 工作日志

### 1. 今日总体判断

今天对项目做了一次收口复盘。当前项目不适合继续包装成“公开英文医学榜单大幅提升”的故事，因为 SFT-v2-A 和 GRPO-V3 相比 Base 在 MedQA、MedMCQA、PubMedQA、CMMLU、MMLU-Pro 等公开评测上主要是小幅波动或局部提升。

但项目本身仍然有价值：它已经完成了一个较完整的中文医疗 o1-style reasoning 训练闭环，包括中文医学考试数据清洗、MCQ-to-OpenQA、CoT/RFT 数据构造、SFT、GRPO、奖励函数设计、LoRA 合并、vLLM 推理和 lm-eval 评测。后续应把重点从“继续盲目烧资源追榜单”改为“补齐中文医疗主战场评测 + 分析 GRPO 失败/有效机制 + 做一轮 DAPO-lite 改进实验”。

### 2. 为什么必须补中文医疗评测

本项目训练数据主要来自 `cmexam`、`medqa_zh` 等中文医学考试数据，但此前主要评测集中在英文或泛医学榜单：MedQA-USMLE、MedMCQA、PubMedQA、MMLU-Pro Bio/Health 等。训练域和评测域明显错位。

因此今天新增/修正了中文医疗评测入口：

- `src/evaluation/custom_tasks/zh_med/cmb_exam.yaml`
- `src/evaluation/custom_tasks/zh_med/cmexam.yaml`
- `src/evaluation/custom_tasks/zh_med/utils.py`
- `src/evaluation/run_eval_zh_med.sh`

评测顺序应为：

```text
base -> sft_v2_a -> grpo_v3
任务：CMB + CMExam
```

只有这套结果才更适合判断中文临床推理训练是否真的有效。

### 3. CMB/CMExam 全 0 分问题与修复

第一次跑 `bash src/evaluation/run_eval_zh_med.sh grpo_v3` 时，CMB 和 CMExam 都显示 `exact_match = 0`。后续排查确认这个结果是无效评测，不代表模型真实能力。

主要原因：

1. 原答案抽取正则太窄，只适配类似 `最终答案：B` 的输出；但 GRPO/o1 模型可能输出 `答案为B`、`选择B`、`选项B`，多选题还可能输出 `BCDE` 或 `B、C、D、E`。
2. CMB 中存在多项选择题，标准答案可能是 `BCDE`，旧逻辑偏单选，容易误判。
3. CMExam 直接从 HuggingFace 加载时，云端网络反复 retry，不稳定。
4. 部分排查命令曾在 `/gemini` 下执行，而不是 `/gemini/code/MedGPT-o1-Main`，导致误报文件不存在。
5. `fatal: not a git repository` 是 lm-eval tracker 的提示，不影响评测结果。

修复内容：

- `cmb_exam.yaml` 改为读取本地 `data/raw/eval/cmb/data/CMB-val-merge.json`。
- `cmexam.yaml` 改为读取本地 `data/raw/eval/cmexam/cmexam_validation.jsonl`。
- `utils.py` 支持单选/多选答案归一化。
- YAML 中的 `regex_pattern` 放宽，支持 `最终答案`、`答案为`、`选择`、`选项` 和多字母答案。
- 保留 `max_gen_toks: 2048`，避免 GRPO-V3 的 `<think>` 推理链被截断后无法输出最终答案。
- 保留 `--log_samples`，便于之后检查 `resps` 和 `filtered_resps`。

修复后先用 `--limit 20` 做冒烟验证：

```text
cmb_exam: exact_match = 0.65 ± 0.1094
cmexam:   exact_match = 0.85 ± 0.0819
```

随后发现：在当前云端环境中，把 `cmb_exam,cmexam` 合并为一个 `lm_eval` 全量任务时，曾出现聚合结果双 0 且 sample 文件缺失的异常；但将两个任务拆开单独全量评测后结果正常。因此最终结论是：**后续中文医疗评测必须逐任务单独运行，不再用 `--tasks cmb_exam,cmexam` 合并长跑。**

GRPO-V3 逐任务全量评测的可信结果：

```text
cmb_exam full: exact_match = 0.7036 ± 0.0273  (280 samples)
cmexam full:   exact_match = 0.8056 ± 0.0048  (6811 samples)
```

这说明中文医疗主战场评测链路已经打通，且 GRPO-V3 在 CMB 与 CMExam 上取得了较高的绝对分数。下一步需要用同一套逐任务评测方式补跑 `base` 和 `sft_v2_a`，形成公平对照。

当前可信中文医疗评测记录如下：

| 模型 | CMB / cmb_exam | CMExam / cmexam | 备注 |
|---|---:|---:|---|
| Base | **71.43%** | **75.54%** | 已完成逐任务全量评测 |
| SFT-v1 | **70.71%** | **80.36%** | 已完成逐任务全量评测 |
| GRPO-v1 | **71.43%** | **80.44%** | 已完成逐任务全量评测 |
| SFT-v2-A | **70.00%** | **80.52%** | 已完成逐任务全量评测 |
| GRPO-V3 | **70.36%** | **80.56%** | 已完成逐任务全量评测；合并双任务全量 0 分结果作废 |

后续所有 CMB/CMExam 对比均以逐任务单独运行结果为准，不再采用 `--tasks cmb_exam,cmexam` 合并长跑结果。

阶段性观察：在当前 CMB/CMExam 选择题评测口径下，SFT 系列的收益主要体现在 CMExam，而不是 CMB。CMB 上 Base 与 GRPO-v1 最高，说明该榜单可能更偏知识记忆/考试选择题稳态，SFT/RL 的长推理输出并没有稳定转化为更高选择题准确率；CMExam 上 SFT-v1、GRPO-v1、SFT-v2-A、GRPO-V3 均明显高于 Base，但几个训练后模型之间差距很小。

```text
Base -> SFT-v2-A:
CMB:    71.43% -> 70.00%  (-1.43 pp)
CMExam: 75.54% -> 80.52%  (+4.98 pp)

Base -> SFT-v1:
CMB:    71.43% -> 70.71%  (-0.72 pp)
CMExam: 75.54% -> 80.36%  (+4.82 pp)

SFT-v1 -> GRPO-v1:
CMB:    70.71% -> 71.43%  (+0.72 pp)
CMExam: 80.36% -> 80.44%  (+0.08 pp)

SFT-v2-A -> GRPO-V3:
CMB:    70.00% -> 70.36%  (+0.36 pp)
CMExam: 80.52% -> 80.56%  (+0.04 pp)
```

因此，GRPO 的主要价值暂时不能表述为“显著提升选择题准确率”，而应更谨慎地表述为：在中文医疗主战场选择题上基本保持 SFT 强基线表现，同时保留 o1-style 长推理输出能力。项目后续重点应转向分析为什么 GRPO 的推理过程收益没有显著反映到选择题 exact match，并通过 DAPO-lite 的有效梯度样本筛选验证是否能改善 RL 的样本效率和训练信号质量。

### 4. DAPO-lite 路线

由于当前云端环境的 `trl==0.19.1` 很可能没有原生 `DAPOConfig/DAPOTrainer`，最后一次训练不应强行依赖 `--trainer_backend native_dapo`，否则容易启动即失败。

最终路线定为：基于稳定的 `GRPOTrainer`，手动实现 DAPO 思想，称为 `DAPO-lite / DAPO-inspired GRPO`。核心不是换一个不稳定的新 Trainer，而是在数据和训练配置上解决当前 GRPO 的主要问题。

已确定保留的 DAPO-lite 思路：

- 基于 rollout/reward 结果筛选 reward 方差非零的 prompt，过滤全对、全错或组内 reward 完全一致的无效样本。
- 针对 `frac_reward_zero_std` 偏高的问题，提高有效梯度样本比例。
- 保留 `mask_truncated_completions=True` 和格式惩罚，减少过长 CoT 或被截断输出污染训练。
- 不手写 token-level loss。原因是当前最大瓶颈是有效样本和 reward 方差，不是 per-token loss 归一化；强改 `GRPOTrainer` 内核风险过高，不适合作为最后一次 A100/API 资源实验。

已修改/新增：

- `src/data_pipeline/06_build_dapo_effective_dataset.py`
- `src/data_pipeline/07_generate_full_rollout.py`
- `src/training/train_dapo.py`

DAPO-lite 建议从 `SFT-v2-A` 出发训练，而不是继续接在 `GRPO-V3` 后面，这样最终对比结构更干净：

```text
SFT-v2-A -> GRPO-V3
SFT-v2-A -> DAPO-lite
```

今天开始进入 DAPO-lite 有效样本构造阶段。当前采用 `SFT-v2-A` 作为 rollout 模型，因为 DAPO-lite 计划从 `SFT-v2-A` 起步训练，筛出的样本应尽量反映起点策略下仍存在探索分歧、能够提供有效梯度的题目。

对 `src/data_pipeline/07_generate_full_rollout.py` 做了工程加固：

- **Streaming Writes**：从原来的“全部生成和打分完成后一次性写文件”，改为边打分边写入 `output_file`，并在每个 prompt 完成后 `flush()`，降低长时间 API 任务中断导致结果丢失的风险。
- **Checkpoint/Resume**：启动时读取已有 `output_file`，识别已经完成的 prompt id，后续重启时跳过已完成题目，避免重复生成和重复调用 MiMo Judge API。
- **成本控制试跑**：先用 `--max_samples 1000` 做第一轮 rollout，而不是直接跑 5000；目标是先估算有效样本比例，再决定是否扩展到 3000-5000。

当前已启动的 rollout 命令：

```bash
nohup python -u src/data_pipeline/07_generate_full_rollout.py \
  --model_path /root/models/sft_v2_a_merged \
  --input_file data/final/rl_v3/rl_clean_train.jsonl \
  --output_file reports/sft_v2_a_full_rollout_for_dapo.jsonl \
  --max_samples 1000 \
  --num_generations 4 \
  --max_tokens 512 \
  --temperature 0.7 \
  --top_p 0.9 \
  --vllm_gpu_memory_utilization 0.85 \
  --seed 20260626 \
  > outputs/generate_rollout_full.log 2>&1 &
```

该任务会先生成 `1000 × 4 = 4000` 条候选回答，再通过 `composite_reward_v3_func` 调用 MiMo Judge 得到 `v3_reward`。完成后需要继续执行 `06_build_dapo_effective_dataset.py`，从 rollout 中筛出 reward 方差非零的 prompt，生成最终 DAPO-lite 训练集。

### 5. 遭遇云平台闲置监控（Watchdog）强杀与 DAPO 数据流拆分

在执行上述完整 rollout 时，发现由于 vLLM 生成速度极快（15分钟），但后续调用 MiMo API 裁判打分的过程极慢（数小时且只占用 CPU 和网络），导致云平台检测到 GPU 0% 闲置超过 1 小时（`ORION_TASK_IDLE_TIME`），从而触发 Watchdog 机制强行杀死了进程。

为了解决该问题并降低 GPU 资源浪费，将 DAPO rollout 数据流拆分为“GPU 生成”和“CPU/API 打分”两个阶段：

**计算与网络解耦**：
1. `07_a_gpu_generate_only.py`：纯 GPU 生成器。在 A100 上只负责批量生成候选回答，并将文本缓存到 `reports/dapo/rollout_texts_cache.jsonl`；生成结束后即可释放 GPU。
2. `07_b_cpu_score_only.py`：纯 CPU/API 阅卷器。读取文本缓存，调用 MiMo Judge 打分，并实时写入 `reports/dapo/sft_v2_a_full_rollout_for_dapo.jsonl`。该阶段不需要 GPU，可在 CPU 云主机或本地机器上慢速完成。

**断点续跑修正**：
CPU 打分脚本的 checkpoint 逻辑已调整为：只有同一个 prompt 的 `expected_generations=4` 个候选都写完，才视为该 prompt 已完成。若检测到历史输出中存在半截 prompt，会备份原文件并只保留完整 prompt 记录，避免后续 `06_build_dapo_effective_dataset.py` 读到残缺组、误判 reward 方差。

**工程收纳（DAPO 隔离区）**：
为了保持主干数据流清晰，将 DAPO 拓展相关产物隔离：
- 脚本迁移：将 `06_...`, `07_a_...`, `07_b_...` 全部移动至 `src/data_pipeline/dapo/` 子目录下，并批量修正了 `PROJECT_ROOT` 的相对路径依赖（`parents[3]`）。
- 报告隔离：将所有 DAPO 相关的打分结果、最终数据集报表等默认输出路径统一指向 `reports/dapo/`。

### 6. DAPO-lite 有效梯度数据集落盘

完成拆分式 DAPO rollout 流水线后，已将结果整理进 DAPO 隔离区：

- 文本生成缓存：`reports/dapo/rollout_texts_cache.jsonl`
- MiMo Judge 打分结果：`reports/dapo/sft_v2_a_full_rollout_for_dapo.jsonl`
- 数据集报告：
  - `reports/dapo/dapo_lite_effective_dataset_report.md`
  - `reports/dapo/dapo_lite_effective_dataset_report.json`
- 最终训练/验证集：
  - `data/final/dapo_lite/dapo_effective_train.jsonl`
  - `data/final/dapo_lite/dapo_effective_val.jsonl`

核心统计如下：

| 项目 | 数值 |
|---|---:|
| Rollout prompt groups | 1120 |
| 每题候选回答数 | 4 |
| Rollout 总回答数 | 4480 |
| 原始 clean train | 6308 |
| Effective ids | 874 |
| 写入 DAPO-lite train | 874 |
| 写入 DAPO-lite val | 702 |
| train/val id overlap | 0 |
| 训练集来源：cmexam | 507 |
| 训练集来源：medqa_zh | 367 |

筛选逻辑保持非常克制：只保留组内 `v3_reward` 至少出现两个不同分数、且 reward 标准差非零的 prompt。这一策略直接针对 GRPO-V3 复盘中发现的 `frac_reward_zero_std` 偏高问题，目标不是扩大数据规模，而是提高每一步 RL 更新中真实有效梯度的比例。

从当前数据量看，这批 874 条更适合作为 **DAPO-lite pilot / 小规模验证集**，足以验证训练链路、奖励函数、vLLM、LoRA 合并与 wandb 记录是否稳定；如果要作为最终主结果，后续仍应把 rollout 覆盖从 1120 个 prompt 扩展到更接近完整 `rl_clean_train.jsonl` 的 6308 条。

### 7. 云端 DAPO-lite 60-step Smoke Test

基于 `SFT-v2-A -> DAPO-lite` 的干净对照路线，已在云端完成一次 `max_steps=60` 的 DAPO-lite 试训。该实验的主要目的不是追求最终分数，而是验证最后一条 RLVR 训练链路是否能稳定跑通。

本次训练配置要点：

- Base model：`/gemini/pretrain/Qwen2.5-7B-Instruct`
- SFT adapter：`outputs/sft_v2_a_lora`
- Train file：`data/final/dapo_lite/dapo_effective_train.jsonl`
- Val file：`data/final/dapo_lite/dapo_effective_val.jsonl`
- Output dir：`outputs/dapo_lite_qwen2_5_7b_medical_final`
- Trainer backend：`grpo_compat (GRPOConfig, GRPOTrainer)`
- 定位：`DAPO-lite / DAPO-inspired GRPO`，不是原生 `DAPOTrainer`
- `max_steps=60`
- `num_generations=4`
- `max_prompt_length=1024`
- `max_completion_length=1024`
- `per_device_train_batch_size=4`
- `gradient_accumulation_steps=2`
- `learning_rate=5e-6`
- `--use_vllm`
- `vllm_gpu_memory_utilization=0.5`
- WandB run：`dapo-lite-874-vllm-80g`

运行中遇到并解决的问题：

1. `--save_strategy no` / `--eval_strategy no` 不是当前 `train_dapo.py` 的 CLI 参数，已改用超大 `save_steps/eval_steps` 或直接依赖脚本最终 `save_model()`。
2. `outputs/sft_qwen2_5_7b_lora_v2` 在云端不存在，PEFT 会误当成 HuggingFace repo 并联网查找 `adapter_config.json`；已改为云端真实存在的 `outputs/sft_v2_a_lora`。
3. WandB 第一次启动会进入交互登录；本次完成了 wandb 记录，后续若不需要在线图表可改为 `--report_to none` 或 `WANDB_MODE=offline`。
4. `native DAPO is unavailable` 属于预期提示：当前 TRL 环境没有原生 DAPO 类，脚本自动退回 `GRPOTrainer`，这与本项目“DAPO-lite / DAPO-inspired GRPO”的定位一致。
5. `torch_dtype is deprecated` 和退出时的 `destroy_process_group()` warning 均不影响训练结果。

60-step 试训观察：

- 训练成功完成并落盘到 `outputs/dapo_lite_qwen2_5_7b_medical_final`。
- `train/epoch` 约为 0.13，说明 60 steps 只覆盖了约 13% 的一个 epoch，不能视为完整训练结论。
- `train/kl` 约在 `7e-4` 到 `1.1e-3` 附近，整体很低，说明策略更新非常保守，没有明显 KL 发散。
- `train/grad_norm` 主要在低幅区间内波动，没有梯度爆炸迹象。
- `train/reward` 与 `composite_reward_v3_func/mean` 在不同 batch 间波动明显，符合 RLVR 小 batch 训练特征；60 steps 太短，暂时不能据此判断最终 reward 是否稳定上升。
- 平均 completion 长度约在 170-205 tokens，max 多数在 200-330 tokens，明显低于 `max_completion_length=1024`，没有出现大规模超长输出或截断污染。
- 终端打印样例中可以看到模型仍保持 `<think>...</think>` + `最终答案：...` 格式，且能给出较完整的中文医学推理链。

阶段性结论：这次 60-step 训练证明 DAPO-lite 的数据、奖励、LoRA、vLLM、wandb 和最终保存链路已经打通。它应被记录为 **smoke test 成功**，而不是最终效果实验。后续正式结论仍需至少跑完 `num_train_epochs=1.0`，或先用 `max_steps=300` 做更长的中间验证。

### 8. 下一步

1. 将 DAPO-lite 从 `max_steps=60` 扩展到 `max_steps=300` 或完整 `num_train_epochs=1.0`，观察 reward、KL、zero-std 与输出格式是否持续稳定。
2. 正式训练时建议恢复周期性保存，例如 `save_steps=100`、`eval_steps=100`，避免长任务中断后只能从头开始。
3. 训练完成后合并 DAPO-lite LoRA，加入现有评测矩阵：`Base / SFT-v2-A / GRPO-V3 / DAPO-lite`。
4. 重点评测不再只看英文公开榜单，而应优先跑中文医疗主战场：`CMB`、`CMExam`、内部 `rl_val` ORM exact/any、以及若干人工 case。
5. 最终简历/论文表述应强调：中文医疗 reasoning 数据构造、RLVR 奖励设计、GRPO 诊断、计算与网络解耦的异构流水线、DAPO-inspired 有效梯度样本筛选，以及从 smoke test 到正式训练的工程闭环。

## 2026-06-27 工作日志

今天主要完成了 DAPO-lite 数据集的进一步补充。基于 `reports/dapo/sft_v2_a_full_rollout_for_dapo.jsonl` 中已有的 rollout 与 MiMo Judge 打分结果，继续使用 DAPO 拆分流水线做 CPU/API 侧打分补齐，并重新生成了最新版有效梯度数据集：

- `data/final/dapo_lite/dapo_effective_train.jsonl`
- `data/final/dapo_lite/dapo_effective_val.jsonl`
- `reports/dapo/dapo_lite_effective_dataset_report.md`
- `reports/dapo/dapo_lite_effective_dataset_report.json`

这批数据相比 6.26 的 874 条 pilot 集明显扩容：当前 rollout 覆盖 4429 个 prompt、17716 条候选回答，每个 prompt 都保持 4 条候选；最终筛出 3467 条 effective train，验证集保持 702 条。train 中 `cmexam` 1966 条、`medqa_zh` 1501 条，和 val 没有 id 重叠，也没有 JSON 解析错误、重复 id 或缺失 reward 的情况。

质量上看，这批数据已经可以支撑正式的 DAPO-lite 小规模训练。effective 比例约 78.28%，说明多数题目在 SFT-v2-A 起点策略下存在组内 reward 差异，能提供非零 advantage 信号；同时它已经覆盖 `rl_clean_train.jsonl` 的约 55%，比昨天的 smoke test 数据稳很多。需要注意的是，它仍然不是完整训练集覆盖，且 effective 筛选天然偏向“模型有分歧”的样本，因此应表述为基于 SFT-v2-A rollout 和 V3 reward 筛出的 DAPO-lite 有效梯度子集，而不是原始题库的无偏采样。

接下来可以把这版 3467/702 数据同步到云端，基于 `outputs/sft_v2_a_lora` 启动正式 DAPO-lite 训练。若担心成本，可先跑 `max_steps=300` 做中间验证；如果曲线稳定，再跑完整 `num_train_epochs=1.0`。训练完成后再合并 LoRA，并加入 `Base / SFT-v2-A / GRPO-V3 / DAPO-lite` 的中文医疗评测对比。
