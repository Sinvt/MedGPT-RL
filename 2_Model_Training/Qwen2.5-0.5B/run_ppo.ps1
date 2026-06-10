<#
【PPO 参数说明手册】
(因篇幅限制移至此处，所有核心参数均通过数组方式传入以确保 Windows 下的绝对稳定)
#>

$env:CUDA_VISIBLE_DEVICES="0"
$env:PYTHONIOENCODING="utf-8"

# 直接指定 Python 解释器路径，完美避开 conda run 的中文乱码/GBK编码报错 Bug！
$python_exe = "D:\Anaconda\envs\transformers\python.exe"

Write-Host "【第一步】：合并 SFT 模型..."
$sft_args = @(
    "--base_model", "Qwen/Qwen2.5-0.5B",
    "--lora_model", "outputs-sft-qwen-0.5b",
    "--output_dir", "merged-sft-qwen-0.5b"
)
& "D:\Anaconda\envs\transformers\python.exe" tools/merge_peft_adapter.py @sft_args

Write-Host "【第二步】：合并 RM 模型..."
$rm_args = @(
    "--base_model", "merged-sft-qwen-0.5b",
    "--lora_model", "outputs-rm-qwen-0.5b",
    "--output_dir", "merged-rm-qwen-0.5b"
)
& "D:\Anaconda\envs\transformers\python.exe" tools/merge_peft_adapter.py @rm_args

Write-Host "【第三步】：正式启动 PPO..."
$ppo_args = @(
    "--sft_model_path", "merged-sft-qwen-0.5b",
    "--reward_model_path", "merged-rm-qwen-0.5b",
    "--model_name_or_path", "merged-sft-qwen-0.5b",
    "--dtype", "bfloat16",
    "--train_file_dir", "./data/sft",
    "--validation_file_dir", "./data/sft",
    "--max_source_length", "512",
    "--max_completion_length", "128",
    "--per_device_train_batch_size", "1",
    "--gradient_accumulation_steps", "8",
    "--gradient_checkpointing", "True",
    "--use_peft", "True",
    "--do_train",
    "--max_steps", "50",
    "--output_dir", "outputs-ppo-qwen-0.5b",
    "--eval_strategy", "steps",
    "--eval_steps", "20",
    "--num_train_epochs", "1",
    "--learning_rate", "1e-5",
    "--lr_scheduler_type", "cosine",
    "--report_to", "tensorboard"
)
& "D:\Anaconda\envs\transformers\python.exe" training/ppo_training.py @ppo_args
