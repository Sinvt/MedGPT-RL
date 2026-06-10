#!/bin/bash

# 判断是否传入了参数
if [ -z "$1" ]; then
    echo "❌ 错误：请提供要测试的模型名称！"
    echo "用法：bash run_eval_7b_mmlu_pro.sh <模型名称>"
    echo "示例：bash run_eval_7b_mmlu_pro.sh base"
    exit 1
fi

MODEL_TYPE=$1

# 设置 Hugging Face 国内镜像源
export HF_ENDPOINT="https://hf-mirror.com"
export CUDA_VISIBLE_DEVICES="0"

# 根据参数拼接路径
if [ "$MODEL_TYPE" == "base" ]; then
    MODEL_PATH="/gemini/pretrain/Qwen2.5-7B-Instruct"
elif [ "$MODEL_TYPE" == "sft" ]; then
    MODEL_PATH="/gemini/code/2_Model_Training/Qwen2.5-7B/merged-sft-qwen-7b"
elif [ "$MODEL_TYPE" == "ppo" ]; then
    MODEL_PATH="/gemini/code/2_Model_Training/Qwen2.5-7B/merged-ppo-qwen-7b"
else
    echo "❌ 错误: 请指定正确的模型类型 (base/sft/ppo)!"
    exit 1
fi

echo "======================================================="
echo "🚀 开始进行医学榜单 MMLU-Pro 专属评测"
echo "🤖 当前测试模型: $MODEL_TYPE"
echo "📁 模型读取路径: $MODEL_PATH"
echo "⚠️ 注意：该任务涉及生成式推导 (CoT)，运行耗时约 2-3 小时"
echo "======================================================="

lm_eval --model hf \
    --model_args pretrained=$MODEL_PATH,dtype=bfloat16 \
    --tasks mmlu_pro_biology,mmlu_pro_health \
    --batch_size 4 \
    --trust_remote_code

echo "✅ $MODEL_TYPE MMLU-Pro 评测结束！"
