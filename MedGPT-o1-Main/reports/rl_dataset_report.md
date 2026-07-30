# RL Dataset Report

## 输入输出

- 输入文件：`D:\Vscode\Project\Medical-GPT\MedGPT-o1-Main\data\intermediate\openqa_raw.jsonl`
- 输出目录：`D:\Vscode\Project\Medical-GPT\MedGPT-o1-Main\data\final\rl`
- 训练集：`D:\Vscode\Project\Medical-GPT\MedGPT-o1-Main\data\final\rl\rl_train.jsonl`
- 验证集：`D:\Vscode\Project\Medical-GPT\MedGPT-o1-Main\data\final\rl\rl_val.jsonl`
- 验证集比例：0.02
- 随机种子：42

## 数据统计

- 原始读取条数：7159
- 有效 RL 样本数：7159
- 跳过样本数：0
- 来源分布：{'cmexam': 4030, 'medqa_zh': 3129}

## 训练/验证切分

- train 样本数：7016
- train 来源分布：{'medqa_zh': 3073, 'cmexam': 3943}
- val 样本数：143
- val 来源分布：{'cmexam': 87, 'medqa_zh': 56}

## 数据格式

每条样本包含 `prompt`、`question`、`standard_answer`、`answer_aliases` 等字段。后续 GRPO/DAPO 只把 `prompt` 喂给模型生成回答，`standard_answer` 和 `answer_aliases` 用于 ORM 奖励计算。
