# 运行方式: bash run_merge_rm_7b.sh

export CUDA_VISIBLE_DEVICES="0"
export PYTHONPATH=$PWD:$PYTHONPATH

# 把 RM 的 LoRA 权重合并进上一步的基座中，生成供 PPO 阶段加载的完整裁判模型
python tools/merge_peft_adapter.py \
    --base_model "merged-sft-qwen-7b" \
    --lora_model "outputs-rm-qwen-7b" \
    --output_dir "merged-rm-qwen-7b"
