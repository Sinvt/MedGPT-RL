#!/bin/bash

# 【SFT 7B 训练脚本 (云端 Linux 专用)】
# 运行方式: bash run_sft_7b.sh

export CUDA_VISIBLE_DEVICES="0"
export PYTHONPATH=$PWD:$PYTHONPATH

# 如果遇到显存OOM，可以尝试将 per_device_train_batch_size 减小到 2，gradient_accumulation_steps 增加到 8
python training/supervised_finetuning.py \
    --model_name_or_path "/gemini/pretrain/Qwen2.5-7B-Instruct" \
    --train_file_dir "./data/sft" \
    --validation_file_dir "./data/sft" \
    --per_device_train_batch_size 2 \
    --per_device_eval_batch_size 2 \
    --do_train \
    --do_eval \
    --use_peft True \
    --gradient_checkpointing True \
    --bf16 True \
    --model_max_length 2048 \
    --num_train_epochs 3 \
    --learning_rate 2e-5 \
    --lr_scheduler_type "cosine" \
    --warmup_steps 50 \
    --weight_decay 0.05 \
    --logging_strategy "steps" \
    --logging_steps 10 \
    --eval_steps 100 \
    --eval_strategy "steps" \
    --save_steps 200 \
    --save_strategy "steps" \
    --save_total_limit 3 \
    --gradient_accumulation_steps 8 \
    --preprocessing_num_workers 8 \
    --output_dir "outputs-sft-qwen-7b"
