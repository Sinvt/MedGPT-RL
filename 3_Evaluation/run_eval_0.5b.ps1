param (
    [Parameter(Mandatory = $true)]
    [ValidateSet("base", "sft", "ppo")]
    [string]$ModelType
)

if ($ModelType -eq "base") {
    $ModelPath = "Qwen/Qwen2.5-0.5B"
}
elseif ($ModelType -eq "sft") {
    $ModelPath = "D:\Vscode\Project\Medical-GPT\2_Model_Training\Qwen2.5-0.5B\merged-sft-qwen-0.5b"
}
elseif ($ModelType -eq "ppo") {
    $ModelPath = "D:\Vscode\Project\Medical-GPT\2_Model_Training\Qwen2.5-0.5B\merged-ppo-qwen-0.5b"
}

Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host "🚀 开始进行 0.5B 医学榜单 Benchmark 评测 (Windows本地)" -ForegroundColor Cyan
Write-Host "🤖 当前测试模型: $ModelType" -ForegroundColor Cyan
Write-Host "📁 模型读取路径: $ModelPath" -ForegroundColor Cyan
Write-Host "=======================================================" -ForegroundColor Cyan

$env:CUDA_VISIBLE_DEVICES = "0"
$env:HF_ENDPOINT = "https://hf-mirror.com"
$env:HF_DATASETS_TRUST_REMOTE_CODE = "1"

lm_eval --model hf --model_args pretrained=$ModelPath,dtype=bfloat16,trust_remote_code=True --tasks cmmlu_anatomy,cmmlu_clinical_knowledge,cmmlu_college_medicine,cmmlu_genetics,cmmlu_professional_medicine,cmmlu_traditional_chinese_medicine,cmmlu_virology,pubmedqa,medqa_4options,medmcqa --device cuda:0 --batch_size 16 --trust_remote_code --output_path D:\Vscode\Project\Medical-GPT\3_Evaluation\results

Write-Host "[OK] $ModelType (0.5B) Eval Complete!" -ForegroundColor Green
