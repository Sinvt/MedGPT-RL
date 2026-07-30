import argparse
import gc
import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

def merge_lora(base_model_path: str, lora_path: str, output_path: str):
    print(f"========== 开始合并 LoRA ==========")
    print(f"Base 模型: {base_model_path}")
    print(f"LoRA 模型: {lora_path}")
    print(f"输出路径: {output_path}")

    # 1. 加载 Tokenizer
    tokenizer = AutoTokenizer.from_pretrained(base_model_path, trust_remote_code=True)
    
    # 2. 加载 Base 模型
    print("加载 Base 模型中...")
    base_model = AutoModelForCausalLM.from_pretrained(
        base_model_path,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
    )

    # 3. 加载并挂载 LoRA
    print("加载 LoRA adapter...")
    model = PeftModel.from_pretrained(base_model, lora_path)

    # 4. 执行合并
    print("执行 merge_and_unload (物理合并权重)...")
    model = model.merge_and_unload()

    # 5. 保存
    print("保存合并后的权重...")
    model.save_pretrained(output_path, max_shard_size="5GB")
    tokenizer.save_pretrained(output_path)
    
    print("✅ 合并完成！")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=str, default="/gemini/pretrain/Qwen2.5-7B-Instruct")
    parser.add_argument("--lora", type=str, default="outputs/sft_qwen2_5_7b_lora_v1")
    parser.add_argument("--output", type=str, default="outputs/sft_merged_v1")
    args = parser.parse_args()
    merge_lora(args.base, args.lora, args.output)
