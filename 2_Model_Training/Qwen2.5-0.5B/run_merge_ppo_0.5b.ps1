$env:CUDA_VISIBLE_DEVICES="0"
$env:PYTHONPATH="D:\Vscode\Project\Medical-GPT\2_Model_Training;" + $env:PYTHONPATH

Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host "Starting PPO Merge..." -ForegroundColor Cyan
Write-Host "=======================================================" -ForegroundColor Cyan

python D:\Vscode\Project\Medical-GPT\2_Model_Training\tools\merge_peft_adapter.py --base_model "D:\Vscode\Project\Medical-GPT\2_Model_Training\Qwen2.5-0.5B\merged-sft-qwen-0.5b" --lora_model "D:\Vscode\Project\Medical-GPT\2_Model_Training\Qwen2.5-0.5B\outputs-ppo-qwen-0.5b" --output_dir "D:\Vscode\Project\Medical-GPT\2_Model_Training\Qwen2.5-0.5B\merged-ppo-qwen-0.5b"

Write-Host "[OK] PPO Merge Complete!" -ForegroundColor Green
