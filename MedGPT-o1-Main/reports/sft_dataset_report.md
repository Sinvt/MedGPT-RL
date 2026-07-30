# SFT Dataset Report

## 输入输出

- 输入文件：`D:\Vscode\Project\Medical-GPT\MedGPT-o1-Main\data\intermediate\cot_filtered.jsonl`
- 训练集：`D:\Vscode\Project\Medical-GPT\MedGPT-o1-Main\data\final\sft\sft_train.jsonl`
- 验证集：`D:\Vscode\Project\Medical-GPT\MedGPT-o1-Main\data\final\sft\sft_val.jsonl`
- 全量选中样本：`D:\Vscode\Project\Medical-GPT\MedGPT-o1-Main\data\final\sft\sft_all.jsonl`
- 每题保留 CoT 数：1
- 验证集比例：0.1

## 输入数据

- CoT 总数：8800
- 覆盖题目数：6880
- 来源分布：cmexam: 5032，medqa_zh: 3768

## 选中数据

- SFT 样本数：6880
- 覆盖题目数：6880
- 来源分布：cmexam: 3873，medqa_zh: 3007
- 平均推理步骤数：4.99
- 平均 `<think>` 内容长度：245.99

## 训练/验证切分

- train 样本数：6192
- train 来源分布：cmexam: 3482，medqa_zh: 2710
- val 样本数：688
- val 来源分布：cmexam: 391，medqa_zh: 297
- 切分方式：按题目分组切分，避免同一道题同时出现在 train 和 val。

## 数据格式

每条样本包含 `messages` 字段，可直接用于 ChatML/ShareGPT 风格 SFT：

```json
{
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "开放式医学问题"},
    {"role": "assistant", "content": "<think>...</think>\n最终答案：..."}
  ]
}
```
