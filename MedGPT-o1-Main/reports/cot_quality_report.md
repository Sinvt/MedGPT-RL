# CoT 质量过滤报告

## 基本信息

- 输入文件：`D:\Vscode\Project\Medical-GPT\MedGPT-o1-Main\data\intermediate\cot_candidates.jsonl`
- 输出文件：`D:\Vscode\Project\Medical-GPT\MedGPT-o1-Main\data\intermediate\cot_filtered.jsonl`
- 拒绝文件：`D:\Vscode\Project\Medical-GPT\MedGPT-o1-Main\data\intermediate\cot_rejected.jsonl`
- 每题最多保留：3 条
- 最低质量分：70.0
- 是否要求 ORM 命中：True
- 是否硬拒绝标注泄漏：True
- 是否硬拒绝选择题痕迹：True
- 重复 id 处理策略：keep_last
- 运行策略：全量重建输出文件，不追加写入

## 总体结果

- 输入候选总数：3026
- 去重后候选数：3026
- 原始覆盖题目数：1009
- 过滤后候选数：2926
- 过滤后覆盖题目数：1006
- 拒绝候选数：100
- 保留率：96.70%

## 来源分布

- cmexam：原始 1820 条，保留 1764 条
- medqa_zh：原始 1206 条，保留 1162 条

## 保留样本形态

- 推理步骤数：平均 4.91，中位数 5.00，最小 3，最大 7
- `<think>` 内容长度：平均 243.67，中位数 238.00，最小 96，最大 544

## 主要拒绝原因

- 疑似标注泄漏：标准答案：98 条
- 疑似选择题痕迹：错误选项：2 条
- 疑似标注泄漏：给定答案：1 条
- 疑似空泛拒答：不确定：1 条
