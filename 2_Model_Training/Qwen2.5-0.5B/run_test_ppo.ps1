$env:CUDA_VISIBLE_DEVICES="0"

python demo/inference.py `
    --base_model merged-sft-qwen-0.5b `
    --lora_model outputs-ppo-qwen-0.5b `
    --interactive
