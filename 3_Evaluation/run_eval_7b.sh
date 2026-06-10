#!/bin/bash

# 运行方式: bash run_eval_7b.sh [base|sft|ppo]
# 例如: bash run_eval_7b.sh sft

MODEL_TYPE=$1

if [ "$MODEL_TYPE" == "base" ]; then
    MODEL_PATH="/gemini/pretrain/Qwen2.5-7B-Instruct"
elif [ "$MODEL_TYPE" == "sft" ]; then
    MODEL_PATH="/gemini/code/2_Model_Training/Qwen2.5-7B/merged-sft-qwen-7b"
elif [ "$MODEL_TYPE" == "ppo" ]; then
    MODEL_PATH="/gemini/code/2_Model_Training/Qwen2.5-7B/merged-ppo-qwen-7b"
else
    echo "❌ 错误: 请指定要评测的模型类型!"
    echo "👉 可用选项: base, sft, ppo"
    echo "💡 示例: bash run_eval_7b.sh ppo"
    exit 1
fi

echo "======================================================="
echo "🚀 开始进行医学榜单 Benchmark 评测"
echo "🤖 当前测试模型: $MODEL_TYPE"
echo "📁 模型读取路径: $MODEL_PATH"
echo "======================================================="

export CUDA_VISIBLE_DEVICES="0"

# 设置 Hugging Face 国内镜像源，防止连不上外网导致 evaluate 指标脚本下载失败
export HF_ENDPOINT="https://hf-mirror.com"

# 允许 Hugging Face 下载并执行远程数据集脚本 (解决 cmmlu.py 报错)
export HF_DATASETS_TRUST_REMOTE_CODE=1

# 使用 bfloat16 精度进行打分，确保评测精度与训练精度完美对齐
lm_eval --model hf \
    --model_args pretrained=$MODEL_PATH,dtype=bfloat16,trust_remote_code=True \
    --tasks cmmlu_anatomy,cmmlu_clinical_knowledge,cmmlu_college_medicine,cmmlu_genetics,cmmlu_professional_medicine,cmmlu_traditional_chinese_medicine,cmmlu_virology,pubmedqa,medqa_4options,medmcqa \
    --device cuda:0 \
    --batch_size 4 \
    --trust_remote_code

echo "✅ $MODEL_TYPE 评测结束！"
