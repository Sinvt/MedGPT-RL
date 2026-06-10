# 运行方式: bash run_merge_ppo_7b.sh

export CUDA_VISIBLE_DEVICES="0"
export PYTHONPATH=$PWD:$PYTHONPATH

# 把 PPO 强化学习跑出来的 LoRA 权重合并进 SFT 基座中，生成最终的完整模型！
python tools/merge_peft_adapter.py \
    --base_model "merged-sft-qwen-7b" \
    --lora_model "outputs-ppo-qwen-7b" \
    --output_dir "merged-ppo-qwen-7b"
