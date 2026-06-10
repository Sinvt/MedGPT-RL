# 运行方式: bash run_rm_7b.sh

export CUDA_VISIBLE_DEVICES="0"
export PYTHONPATH=$PWD:$PYTHONPATH

# 使用合并后的 SFT 模型作为起点，训练打分裁判 (Reward Model)
python training/reward_modeling.py \
    --model_name_or_path "merged-sft-qwen-7b" \
    --train_file_dir "./data/reward" \
    --validation_file_dir "./data/reward" \
    --per_device_train_batch_size 2 \
    --gradient_accumulation_steps 8 \
    --per_device_eval_batch_size 1 \
    --do_train \
    --use_peft True \
    --gradient_checkpointing True \
    --remove_unused_columns False \
    --seed 42 \
    --num_train_epochs 1 \
    --learning_rate 2e-5 \
    --lr_scheduler_type "cosine" \
    --warmup_steps 5 \
    --weight_decay 0.001 \
    --logging_strategy "steps" \
    --logging_steps 10 \
    --eval_steps 50 \
    --eval_strategy "steps" \
    --save_steps 200 \
    --save_strategy "steps" \
    --save_total_limit 3 \
    --max_source_length 2048 \
    --max_target_length 512 \
    --output_dir "outputs-rm-qwen-7b" \
    --logging_first_step True \
    --lora_rank 8 \
    --lora_alpha 16 \
    --lora_dropout 0.05 \
    --bf16 True \
    --report_to "wandb"
