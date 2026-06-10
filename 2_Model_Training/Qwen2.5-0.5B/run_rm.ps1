<#
【RM 参数说明手册】
(由于篇幅限制移至此处，所有参数使用 PowerShell 数组方式传递，保证 100% 不会报错)
#>

$env:CUDA_VISIBLE_DEVICES="0"
$rm_args = @(
    "--model_name_or_path", "merged-sft-qwen-0.5b",
    "--train_file_dir", "./data/reward",
    "--validation_file_dir", "./data/reward",
    "--per_device_train_batch_size", "2",
    "--gradient_accumulation_steps", "8",
    "--per_device_eval_batch_size", "1",
    "--do_train",
    "--use_peft", "True",
    "--remove_unused_columns", "False",
    "--seed", "42",
    "--max_train_samples", "1000",
    "--max_eval_samples", "10",
    "--num_train_epochs", "1",
    "--learning_rate", "2e-5",
    "--lr_scheduler_type", "cosine",
    "--warmup_steps", "5",
    "--weight_decay", "0.001",
    "--logging_strategy", "steps",
    "--logging_steps", "10",
    "--eval_steps", "50",
    "--eval_strategy", "steps",
    "--save_steps", "500",
    "--save_strategy", "steps",
    "--save_total_limit", "3",
    "--max_source_length", "512",
    "--max_target_length", "256",
    "--output_dir", "outputs-rm-qwen-0.5b",
    "--ddp_timeout", "30000",
    "--logging_first_step", "True",
    "--target_modules", "all",
    "--lora_rank", "8",
    "--lora_alpha", "16",
    "--lora_dropout", "0.05",
    "--bf16",
    "--torch_dtype", "bfloat16"
)
& "D:\Anaconda\envs\transformers\python.exe" training/reward_modeling.py @rm_args
