<#
【SFT 参数说明手册】
(由于篇幅限制移至此处，所有参数使用 PowerShell 数组方式传递，保证 100% 不会报错)
#>

$env:CUDA_VISIBLE_DEVICES="0"
$sft_args = @(
    "--model_name_or_path", "Qwen/Qwen2.5-0.5B",
    "--train_file_dir", "./data/sft",
    "--validation_file_dir", "./data/sft",
    "--per_device_train_batch_size", "2",
    "--per_device_eval_batch_size", "1",
    "--do_train",
    "--do_eval",
    "--use_peft", "True",
    "--max_train_samples", "1000",
    "--max_eval_samples", "10",
    "--model_max_length", "512",
    "--num_train_epochs", "1",
    "--learning_rate", "2e-5",
    "--lr_scheduler_type", "cosine",
    "--warmup_steps", "5",
    "--weight_decay", "0.05",
    "--logging_strategy", "steps",
    "--logging_steps", "10",
    "--eval_steps", "50",
    "--eval_strategy", "steps",
    "--save_steps", "500",
    "--save_strategy", "steps",
    "--save_total_limit", "13",
    "--gradient_accumulation_steps", "8",
    "--preprocessing_num_workers", "4",
    "--output_dir", "outputs-sft-qwen-0.5b"
)
conda run -n transformers --live-stream python training/supervised_finetuning.py @sft_args
