# 运行方式: bash run_ppo_7b.sh

export CUDA_VISIBLE_DEVICES="0"
export PYTHONPATH=$PWD:$PYTHONPATH

# 使用 RLOO (一种最新的 PPO 变体) 进行强化学习对齐
python training/ppo_training.py \
    --sft_model_path "merged-sft-qwen-7b" \
    --reward_model_path "merged-rm-qwen-7b" \
    --model_name_or_path "merged-sft-qwen-7b" \
    --dtype "bfloat16" \
    --bf16 True \
    --train_file_dir "./data/sft" \
    --validation_file_dir "./data/sft" \
    --max_source_length 1024 \
    --max_completion_length 256 \
    --per_device_train_batch_size 1 \
    --gradient_accumulation_steps 8 \
    --gradient_checkpointing True \
    --use_peft True \
    --lora_r 8 \
    --lora_alpha 16 \
    --lora_dropout 0.05 \
    --do_train \
    --max_steps 50 \
    --output_dir "outputs-ppo-qwen-7b" \
    --eval_strategy "steps" \
    --eval_steps 20 \
    --learning_rate 1e-5 \
    --lr_scheduler_type "cosine" \
    --report_to "wandb"
