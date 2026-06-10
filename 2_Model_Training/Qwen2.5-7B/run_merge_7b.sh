# 运行方式: bash run_merge_7b.sh

export CUDA_VISIBLE_DEVICES="0"
export PYTHONPATH=$PWD:$PYTHONPATH

# 合并 SFT 训练得到的 LoRA 权重到基座模型中
python tools/merge_peft_adapter.py \
    --base_model "/gemini/pretrain/Qwen2.5-7B-Instruct" \
    --lora_model "outputs-sft-qwen-7b" \
    --output_dir "merged-sft-qwen-7b"
