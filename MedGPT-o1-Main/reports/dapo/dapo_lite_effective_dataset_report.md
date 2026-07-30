# DAPO-lite Effective Dataset Report

- Source train file: `D:\Vscode\Project\Medical-GPT\MedGPT-o1-Main\data\final\rl_v3\rl_clean_train.jsonl`
- Rollout file: `D:\Vscode\Project\Medical-GPT\MedGPT-o1-Main\reports\dapo\sft_v2_a_full_rollout_for_dapo.jsonl`
- Reward field: `v3_reward`
- Rollout groups: 4429
- Source train rows: 6308
- Effective ids: 3467
- Written train rows: 3467
- Missing ids in source train: 0
- min_group_size: 2
- min_std: 1e-06
- min_reward: None
- max_reward: None

## Source Distribution

- cmexam: 1966
- medqa_zh: 1501

## Reward Std Buckets

- zero: 962
- (0,0.05): 46
- [0.05,0.2): 717
- >=0.2: 2704
