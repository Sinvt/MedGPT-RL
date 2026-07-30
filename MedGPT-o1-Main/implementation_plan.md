# MedGPT-o1 执行计划

> 本文档只记录项目主线和各阶段交付物。具体每日进展写入 `PROJECT_LOG.md`。

## 总体目标

在单卡 40G 约束下，跑通一个医疗长推理后训练闭环：

```text
医学选择题数据
  -> 开放式可验证问答 OpenQA
  -> Complex CoT 数据构造
  -> 本地规则过滤 + ORM 校验
  -> SFT 数据集
  -> Qwen2.5-7B-Instruct LoRA SFT
  -> RL 数据集
  -> GRPO 规则奖励训练
  -> DAPO 小规模对比
  -> 统一评测与项目报告
```

当前决策：

- 主线不再依赖 PRM。
- 当前版本不继续维护 PRM 支线；已删除 PRM 标注、PRM 数据集和 PRM 报告文件。
- 在线 RL reward 使用 ORM + 格式 + 规则奖励，不使用 PRM。

## 当前主线

```text
data/raw/train/*.jsonl
  ↓
01_mcq_to_openqa.py
  ↓
data/intermediate/openqa_raw.jsonl
  ↓
02_complex_cot_gen.py
  ↓
data/intermediate/cot_candidates.jsonl
  ↓
02_5_cot_filtering.py
  ↓
data/intermediate/cot_filtered.jsonl
  ↓
03_build_sft_dataset.py
  ↓
data/final/sft_train.jsonl / sft_val.jsonl
  ↓
train_sft.py
  ↓
04_build_rl_dataset.py
  ↓
data/final/rl_train.jsonl / rl_val.jsonl
  ↓
train_grpo.py
  ↓
train_dapo.py
  ↓
evaluation + reports
```

## Phase 0：原始数据下载与校验

目标：获得可用于训练数据构造的中文医学选择题 raw 数据。

脚本：

- `scripts/download_raw_data.py`

输入：

- 公开医学选择题数据源：`cmexam`、`medqa_zh`、`cmb`。

输出：

- `data/raw/train/cmexam_train.jsonl`
- `data/raw/train/medqa_zh_train.jsonl`
- `data/raw/train/cmb_train.jsonl`，如后续能成功获取再加入。

验收：

- JSONL 格式合法。
- `question/options/answer/answer_text/source/split/id` 字段基本齐全。
- 训练集和评测集隔离。
- 无选项、无答案、无法映射答案的样本不进入后续 API 生成。

当前状态：

- 已完成。
- 当前可用 raw 数据：`cmexam 52,741` 条，`medqa_zh 27,327` 条。

## Phase 1：MCQ-to-OpenQA 改写

目标：把医学选择题改写为无选项的开放式可验证问答。

脚本：

- `src/data_pipeline/01_mcq_to_openqa.py`
- `prompts/mcq_to_openqa_prompt.md`

模型：

- `mimo-v2.5`

输入：

- `data/raw/train/cmexam_train.jsonl`
- `data/raw/train/medqa_zh_train.jsonl`

输出：

- `data/intermediate/openqa_raw.jsonl`
- `data/intermediate/openqa_errors.jsonl`

输出样例：

```json
{
  "id": "openqa_cmexam_cmexam_000001",
  "source_id": "cmexam_000001",
  "source": "cmexam",
  "split": "train",
  "question": "开放式医学问题",
  "standard_answer": "标准答案",
  "answer_aliases": ["同义答案"],
  "verifiable": true
}
```

验收：

- 开放题不残留 A/B/C/D 选项。
- `standard_answer` 可被 ORM 校验。
- 支持断点续跑、失败样本落盘、来源混合、token 和耗时统计。

当前状态：

- 已完成。
- 当前产出：`openqa_raw.jsonl` 7,159 条。

## Phase 2：Complex CoT 多路径生成

目标：为 OpenQA 生成带 `<think>` 的医学推理轨迹。

脚本：

- `src/data_pipeline/02_complex_cot_gen.py`
- `prompts/complex_cot_prompt.md`

模型：

- `mimo-v2.5-pro`

输入：

- `data/intermediate/openqa_raw.jsonl`

输出：

- `data/intermediate/cot_candidates.jsonl`
- `data/intermediate/cot_errors.jsonl`

输出样例：

```json
{
  "id": "cot_openqa_cmexam_cmexam_000001_path_0",
  "openqa_id": "openqa_cmexam_cmexam_000001",
  "source_id": "cmexam_000001",
  "source": "cmexam",
  "path_id": 0,
  "question": "开放式医学问题",
  "standard_answer": "标准答案",
  "cot_content": "<think>...</think>\n最终答案：标准答案",
  "final_answer": "标准答案",
  "orm_matched": true
}
```

验收：

- 每题可生成 1-3 条候选 CoT。
- CoT 含 `<think>...</think>` 和 `最终答案：...`。
- 最终答案能通过 ORM 校验。
- 支持断点续跑、来源混合、洗牌抽样、token 和耗时统计。

当前状态：

- 已完成 1K 题规模。
- 当前产出：`cot_candidates.jsonl` 3,026 条，覆盖 1,009 道题。

## Phase 2.5：CoT 本地过滤

目标：用本地规则筛出可进入 SFT 的干净 CoT。

脚本：

- `src/data_pipeline/02_5_cot_filtering.py`

输入：

- `data/intermediate/cot_candidates.jsonl`

输出：

- `data/intermediate/cot_filtered.jsonl`
- `data/intermediate/cot_rejected.jsonl`
- `reports/cot_quality_report.md`

过滤规则：

- ORM 必须命中标准答案。
- 必须有 `<think>...</think>` 和 `最终答案：...`。
- 推理步骤数和长度合理。
- 拒绝标注泄漏，如“标准答案”“给定答案”“根据答案”。
- 拒绝选择题痕迹，如“错误选项”“正确选项”。

验收：

- `cot_filtered.jsonl` 可直接作为 SFT 数据来源。
- `cot_rejected.jsonl` 只作为错误分析或可选负例来源，不进入主线训练。

当前状态：

- 已完成。
- 当前产出：`cot_filtered.jsonl` 2,926 条，覆盖 1,006 道题。

## Phase 3：SFT 数据集构造

目标：从过滤后的 CoT 中构造 SFT 训练数据。

脚本：

- `src/data_pipeline/03_build_sft_dataset.py`

输入：

- `data/intermediate/cot_filtered.jsonl`

输出：

- `data/final/sft_train.jsonl`
- `data/final/sft_val.jsonl`
- `reports/sft_dataset_report.md`

数据格式：

```json
{
  "id": "sft_openqa_cmexam_cmexam_000001",
  "messages": [
    {"role": "system", "content": "你是一个严谨的中文医学推理助手。"},
    {"role": "user", "content": "开放式医学问题"},
    {"role": "assistant", "content": "<think>...</think>\n最终答案：标准答案"}
  ],
  "source": "cmexam",
  "standard_answer": "标准答案",
  "answer_aliases": []
}
```

构造策略：

- 默认每道题保留 1 条质量最高 CoT。
- train/val 按题目分组切分，避免同题泄漏。
- 保留 `source/standard_answer/answer_aliases`，方便后续评测和 RL 构造。

验收：

- SFT train/val 文件生成。
- 格式兼容后续训练脚本。
- 随机抽检样本无选择题泄漏和标准答案泄漏。

当前状态：

- 下一步要做。

## Phase 4：SFT 训练

目标：让 Qwen2.5-7B-Instruct 学会本项目的医疗 CoT 输出格式。

脚本：

- `src/training/train_sft.py`

模型：

- `Qwen2.5-7B-Instruct`

输入：

- `data/final/sft_train.jsonl`
- `data/final/sft_val.jsonl`

输出：

- `outputs/sft/`

训练策略：

- LoRA。
- bf16。
- Gradient Checkpointing。
- 单卡 40G 优先使用保守 batch size。
- 可选 DeepSpeed ZeRO-2。

验收：

- loss 正常下降。
- 推理输出稳定包含 `<think>` 和 `最终答案`。
- 小样本人工检查答案没有明显崩坏。

## Phase 5：RL 数据集与奖励函数

目标：构造 GRPO/DAPO 使用的可验证 prompt 数据和本地奖励函数。

脚本：

- `src/data_pipeline/04_build_rl_dataset.py`
- `src/rewards/orm_reward.py`
- `src/rewards/format_reward.py`
- `src/rewards/process_rule_reward.py`
- `src/rewards/reward_tests.py`

输入：

- `data/intermediate/openqa_raw.jsonl`
- 或 `data/intermediate/cot_filtered.jsonl` 中的 `question/standard_answer/answer_aliases`

输出：

- `data/final/rl_train.jsonl`
- `data/final/rl_val.jsonl`

RL 数据格式：

```json
{
  "id": "rl_openqa_cmexam_cmexam_000001",
  "prompt": "开放式医学问题",
  "standard_answer": "标准答案",
  "answer_aliases": ["同义答案"],
  "source": "cmexam"
}
```

奖励函数：

```text
reward = ORM
       + format reward
       + clinical structure reward
       + length control reward
```

说明：

- ORM 是主奖励，判断最终答案是否匹配标准答案。
- 格式奖励鼓励 `<think>...</think>` 和 `最终答案：...`。
- 规则奖励只作为轻量约束，不替代 ORM。
- PRM 不进入在线 RL reward。

验收：

- 奖励函数有单测。
- 对正确、错误、格式异常、无最终答案等样例能给出合理分数。

## Phase 6：GRPO 与 DAPO 训练

目标：在 SFT 模型基础上跑通在线 RL，并做 DAPO 小规模对比。

脚本：

- `src/training/train_grpo.py`
- `src/training/train_dapo.py`

输入：

- SFT checkpoint。
- `data/final/rl_train.jsonl`
- `data/final/rl_val.jsonl`
- `src/rewards/*`

输出：

- `outputs/grpo/`
- `outputs/dapo/`

训练顺序：

1. 先跑 GRPO sanity check。
2. 确认 reward、KL、entropy、格式合格率正常。
3. 再跑中小规模 GRPO。
4. 最后做 DAPO 小规模对比。

验收：

- 训练不 OOM。
- reward/KL/entropy 可监控。
- 无明显 reward hacking，如复读、只写答案、不写推理。
- DAPO 有同设置小规模对比结果。

## Phase 7：评测与报告

目标：形成可写进 README、报告和简历的实验闭环。

脚本：

- `src/evaluation/run_benchmarks.py`
- `src/evaluation/error_analysis.py`

输入：

- Base 模型。
- SFT checkpoint。
- GRPO checkpoint。
- DAPO checkpoint，可选。

输出：

- `reports/eval_results.md`
- `reports/error_cases.md`
- `reports/cost_report.md`
- `README.md`

评测对象：

- Base。
- SFT。
- GRPO。
- DAPO，可选。

验收：

- 同一评测集、同一 prompt、同一 decoding 参数。
- 有指标表。
- 有错误案例。
- 有成本记录。
- 简历中的每个亮点都有数据或日志支撑。

## 立即下一步：第二轮全面优化 (Round 2 Optimization)

第一轮 `SFT-v1` + `GRPO-v1` 已作为 Baseline 跑通。根据评测发现，模型思考链格式达成率达 100%，但受限于初始正确率，ORM 准确率在 25% 左右。为彻底突破大模型医疗推理的天花板，计划执行以下连贯的数据驱动升级：

### 0. 架构升级：确立“训推解耦”双节点工作流

为了极大加速 RFT 阶段极其耗时的长文本思考链批量生成，同时彻底避免底层通信库依赖冲突，正式确立双物理机协同流水线与严格的模型流转规范：
- **节点 A（训练主节点，当前机器）**：纯净版 PyTorch 2.x + CUDA 12.x。专职负责 SFT、GRPO 模型训练，产出 LoRA / Checkpoint，维持纯净环境。
- **节点 B（推理与评测副节点，新开机器）**：官方预编译 PyTorch 2.1.2 + vLLM 0.4.0。开箱即用，专职负责基于 `vLLM` 引擎的高速公开榜单评测，以及在 RFT 阶段全速并发，**目标生成 3-5 万条候选 CoT**。
- **双节点流转与评测协议**：
  - 节点间仅通过 `data/`、`outputs/*_merged_v1`、`reports/` 同步。训练节点只管训，推理节点只消费合并后的完整模型，绝对物理隔离。
  - **统一走 Merge 模型评测**（因 vLLM 对多层 PEFT 挂载易出坑）：
    - **Base**：`/gemini/pretrain/Qwen2.5-7B-Instruct`
    - **SFT**：合并保存为 `outputs/sft_merged_v1` 后评测
    - **GRPO**：合并保存为 `outputs/grpo_merged_v1` 后评测

### 1. 核心 Baseline 固定
- 跑完当前的 `Base / SFT-v1 / GRPO-v1` 自动化评测矩阵，锁定第一轮成果。后续所有优化均需与此对比。

### 2. 完整的 RFT 与去重流水线 (最优漏斗逻辑)

按照“先自生成放大，再过滤求准，最后向量去重求异”的最优漏斗逻辑，我们的操作和文件流转如下：

**① RFT 并发生成与就地严筛 (防爆显存与断点机制)**
- **操作代码**：`src/data_pipeline/05_rft_rejection_sampling.py`
- **输入文件**：`data/final/rl/rl_train.jsonl` (约 7,000 条原始问题)
- **处理方式**：摒弃“全量生成再过滤”的危险模式。采用 vLLM 分块推理（如 128题/批），每题生成 6 路回答，并在内存中直接调用 `score_response()` 进行最严格的 `exact` ORM 与结构化校验，最后按通过/拒绝立刻分流落盘。
- **输出文件**：`rft_all.jsonl`、`rft_strict_pass.jsonl` (预计剩 1.5 万条左右)、`rft_rejected.jsonl`

**② 本地向量语义去重 (FAISS)**
- **操作代码**：`src/data_pipeline/06_rft_deduplication.py`
- **输入文件**：`data/intermediate/rft/rft_strict_pass.jsonl` (约 15,000 条)
- **处理方式**：先把 `(question_id, candidate_index)` 去重，然后将 `问题 + CoT + 答案` 喂给 `bge-m3` 提取向量，结合 FAISS 剔除余弦相似度极高（如 >0.92）的同质化答卷。
- **最终输出文件**：`data/final/sft/sft_v2_train.jsonl` (最终沉淀出约 5,000~8,000 条极致多样性的黄金思考链)

利用这份终极 `sft_v2_train.jsonl`，我们即可重新训练出更强底盘模型 `SFT-v2`（即 RFT-SFT）。

### 4. 奖励函数全面升级
- 当前奖励偏简单，容易导致“格式对了但答案错”也能拿高分。
- 推荐升级公式：`R = ORM(主导项) + Format + Clinical Structure + Length Control - Repetition Penalty`
- 引入对典型临床推理结构（症状、诊断鉴别）的奖励，以及对机械复读的严格扣分。

### 5. 训练第二轮 GRPO (GRPO-v2)
- 在强大的 `SFT-v2` 底座上，基于升级后的多维奖励函数，再次进行强化学习。
- 此阶段可直接与 `GRPO-v1` 进行严格的消融对比。

### 6. DAPO 探索与终极对决
- 在 `GRPO-v2` 之后引入 `DAPO`，验证其在长推理控制与奖励稳定性上是否能取得更优结果。
- 最终评测矩阵大对决：严格比较 `Base vs SFT-v1 vs GRPO-v1 vs SFT-v2 vs GRPO-v2 vs DAPO` 的核心指标（ORM命中、格式达成、平均奖励、输出长度、复读率等），形成项目完美闭环！
