#!/bin/bash
# 一键评测脚本：集成 Public 与 MMLU-Pro
# 适用场景：加载一次模型，评测所有任务，带有 chat template

set -e

if [ "$#" -ne 1 ]; then
    echo "用法: $0 <模型类型 (base|sft|sft_v2_a|grpo|grpo_v3)>"
    exit 1
fi

MODEL_TYPE=$1

# 统一参数：最大长度为了兼容 MMLU-Pro 设为 8192
COMMON_VLLM_OPTS="dtype=bfloat16,tokenizer_mode=slow,gpu_memory_utilization=0.85,max_model_len=8192,enforce_eager=True,disable_custom_all_reduce=True"

if [ "$MODEL_TYPE" == "base" ]; then
    if [ -d "/root/models/Qwen2.5-7B-Instruct" ]; then
        MODEL_PATH="/root/models/Qwen2.5-7B-Instruct"
    else
        MODEL_PATH="/gemini/pretrain/Qwen2.5-7B-Instruct"
    fi
elif [ "$MODEL_TYPE" == "sft" ]; then
    if [ -d "/root/models/sft_merged_v1" ]; then
        MODEL_PATH="/root/models/sft_merged_v1"
    else
        MODEL_PATH="/gemini/code/MedGPT-o1-Main/outputs/sft_merged_v1"
    fi
elif [ "$MODEL_TYPE" == "sft_v2_a" ]; then
    if [ -d "/root/models/sft_v2_a_merged" ]; then
        MODEL_PATH="/root/models/sft_v2_a_merged"
    else
        MODEL_PATH="/gemini/code/MedGPT-o1-Main/outputs/sft_v2_a_merged"
    fi
elif [ "$MODEL_TYPE" == "grpo" ]; then
    if [ -d "/root/models/grpo_merged_v1" ]; then
        MODEL_PATH="/root/models/grpo_merged_v1"
    else
        MODEL_PATH="/gemini/code/MedGPT-o1-Main/outputs/grpo_merged_v1"
    fi
elif [ "$MODEL_TYPE" == "grpo_v3" ]; then
    if [ -d "/root/models/grpo_merged_v3" ]; then
        MODEL_PATH="/root/models/grpo_merged_v3"
    else
        MODEL_PATH="/gemini/code/MedGPT-o1-Main/outputs/grpo_merged_v3"
    fi
else
    echo "错误：请指定正确的模型类型。"
    echo "可用选项：base, sft, sft_v2_a, grpo, grpo_v3"
    exit 1
fi

MODEL_ARGS="pretrained=${MODEL_PATH},${COMMON_VLLM_OPTS}"

RESULT_DIR="reports/eval_v1_chat_template/${MODEL_TYPE}_all_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESULT_DIR"

echo "======================================================="
echo "开始进行医学大满贯 Benchmark 评测（一键全测）"
echo "当前测试模型：$MODEL_TYPE"
echo "模型参数配置：$MODEL_ARGS"
echo "结果保存目录：$RESULT_DIR"
echo "特性：使用 --apply_chat_template，更加贴合真实对话场景"
echo "======================================================="

export CUDA_VISIBLE_DEVICES="0"
export HF_ENDPOINT="https://hf-mirror.com"
export HF_HUB_ENABLE_HF_TRANSFER=0
export HF_DATASETS_TRUST_REMOTE_CODE=1
# 集成大榜单的所有子集
PUBLIC_TASKS="cmmlu_anatomy,cmmlu_clinical_knowledge,cmmlu_college_medicine,cmmlu_genetics,cmmlu_professional_medicine,cmmlu_traditional_chinese_medicine,cmmlu_virology,pubmedqa,medqa_4options,medmcqa,gsm8k"
MMLU_PRO_TASKS="mmlu_pro_biology,mmlu_pro_health"

ALL_TASKS="${PUBLIC_TASKS},${MMLU_PRO_TASKS}"

lm_eval --model vllm \
    --model_args "$MODEL_ARGS" \
    --tasks "$ALL_TASKS" \
    --batch_size auto \
    --trust_remote_code \
    --apply_chat_template \
    --output_path "$RESULT_DIR"

echo "✅ $MODEL_TYPE 全量榜单评测结束。"
