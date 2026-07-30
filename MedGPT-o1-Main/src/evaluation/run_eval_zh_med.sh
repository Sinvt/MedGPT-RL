#!/bin/bash
# 一键评测脚本：集成中文医疗专属榜单 (CMB, CMExam)
# 注意：当前环境下双任务合并长跑曾出现聚合结果异常，因此默认逐任务单独评测。

set -e

if [ "$#" -ne 1 ]; then
    echo "用法: $0 <模型类型 (base|sft|sft_v2_a|grpo|grpo_v3)>"
    exit 1
fi

MODEL_TYPE=$1

# 统一参数：保持与既有公开榜单评测一致，方便横向比较。
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

RUN_ID="$(date +%Y%m%d_%H%M%S)"
RESULT_ROOT="reports/eval_v1_chat_template/${MODEL_TYPE}_zh_med_${RUN_ID}"
mkdir -p "$RESULT_ROOT"

echo "======================================================="
echo "开始进行专属【中文医疗大满贯】Benchmark 评测"
echo "当前测试模型：$MODEL_TYPE"
echo "模型参数配置：$MODEL_ARGS"
echo "结果保存根目录：$RESULT_ROOT"
echo "任务列表：CMB (Chinese Medical Benchmark), CMExam"
echo "特性：使用 --apply_chat_template，更加贴合真实对话场景"
echo "运行方式：逐任务单独运行，避免多任务全量聚合异常"
echo "======================================================="

export CUDA_VISIBLE_DEVICES="0"
export HF_ENDPOINT="https://hf-mirror.com"
export HF_HUB_ENABLE_HF_TRANSFER=0
export HF_DATASETS_TRUST_REMOTE_CODE=1

CUSTOM_TASK_DIR="src/evaluation/custom_tasks/zh_med"

for TASK in cmb_exam cmexam; do
    TASK_RESULT_DIR="${RESULT_ROOT}/${TASK}"
    mkdir -p "$TASK_RESULT_DIR"

    echo "======================================================="
    echo "开始评测任务：$TASK"
    echo "结果目录：$TASK_RESULT_DIR"
    echo "======================================================="

    lm_eval --model vllm \
        --model_args "$MODEL_ARGS" \
        --include_path "$CUSTOM_TASK_DIR" \
        --tasks "$TASK" \
        --batch_size auto \
        --trust_remote_code \
        --apply_chat_template \
        --log_samples \
        --output_path "$TASK_RESULT_DIR"
done

echo "✅ $MODEL_TYPE 中文医疗榜单评测结束。"
