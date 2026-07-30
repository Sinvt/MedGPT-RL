#!/bin/bash
# ====================================================================
# MedGPT-o1 趋动云环境一键初始化脚本
# 使用方式：bash scripts/prepare_env.sh
# ====================================================================

set -e

echo "=========================================="
echo "  MedGPT-o1 环境初始化"
echo "=========================================="

# 1. 安装 Python 依赖
echo "[1/4] 安装 Python 依赖..."
pip install -r requirements.txt -q
# 强行卸载可能导致依赖冲突的量化库（我们在全血版 BF16 微调中不需要它）
pip uninstall -y bitsandbytes

# 设置 HuggingFace 环境变量，防止内网节点断网卡死
export HF_HUB_OFFLINE=1
export HF_ENDPOINT="https://hf-mirror.com"
export PYTHONUNBUFFERED=1


# 2. 登录 Wandb（需要提前设置环境变量 WANDB_API_KEY）
echo "[2/4] 配置 Wandb..."
if [ -z "$WANDB_API_KEY" ]; then
    echo "  [警告] 未设置 WANDB_API_KEY，请手动执行: wandb login"
else
    wandb login "$WANDB_API_KEY"
    echo "  Wandb 登录成功"
fi

# 3. 检查 GPU 状态
echo "[3/4] 检查 GPU 状态..."
python -c "
import torch
print(f'  PyTorch 版本: {torch.__version__}')
print(f'  CUDA 可用: {torch.cuda.is_available()}')
if torch.cuda.is_available():
    print(f'  GPU 名称: {torch.cuda.get_device_name(0)}')
    print(f'  显存总量: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB')
"

# 4. 自动修复所有 shell 脚本的换行符（防止 Windows 换行符导致 syntax error）
echo "[4/5] 修复 Shell 脚本换行符 (CRLF -> LF)..."
find . -type f -name "*.sh" -exec sed -i 's/\r$//' {} +
echo "  已自动修复全目录下的 .sh 脚本换行符"

# 5. 创建模型软链接（趋动云预装模型路径，按实际情况修改）
echo "[5/5] 检查模型路径..."
MODEL_PATH="/gemini/pretrain/Qwen2.5-7B-Instruct"
if [ -d "$MODEL_PATH" ]; then
    echo "  模型路径存在: $MODEL_PATH"
else
    echo "  [警告] 默认模型路径不存在，请手动指定模型路径"
fi

echo ""
echo "=========================================="
echo "  环境初始化完成！"
echo "=========================================="
