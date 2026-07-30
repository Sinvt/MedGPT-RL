#!/bin/bash
# ====================================================================
# MedGPT 趋动云端一键纯净环境部署脚本 (用于 7B 评测和 vLLM 推理)
# 使用方式：在云端终端里执行 bash scripts/cloud_setup_vllm.sh
# ====================================================================
set -e

echo "=========================================================="
echo "  🚀 趋动云端纯净评测环境自动搭建脚本启动"
echo "=========================================================="

# 1. 确保 Conda 可用
if ! command -v conda &> /dev/null; then
    echo "❌ 错误: 未检测到 Conda，请确保你在趋动云环境配置了 Miniconda 或 Anaconda"
    exit 1
fi

# 2. 移除可能冲突的旧环境
echo "♻️ 清理旧的 medgpt_eval 环境（如果存在）..."
conda remove -n medgpt_eval --all -y || true

# 3. 创建全新的 Python 3.10 环境
echo "🐍 正在创建全新的 Conda 环境: medgpt_eval (Python 3.10)..."
# 为了避免云端网络卡死，这里使用清华源，并避免触发 ToS 问题
conda config --add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main/ || true
conda config --add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/free/ || true
conda config --set auto_update_conda false || true
conda clean -i -y
conda create -n medgpt_eval python=3.10 -y

# 获取 Conda 初始化路径以便在脚本中激活环境
CONDA_BASE=$(conda info --base)
source $CONDA_BASE/etc/profile.d/conda.sh
conda activate medgpt_eval

# 4. 安装底层 PyTorch 和 vLLM
echo "⚙️ 正在通过国内镜像极速安装 PyTorch 2.3.1 和最新 vLLM..."
# 安装指定版本的 torch，并从阿里源拉取，速度飞快
pip install torch==2.3.1 torchvision torchaudio --index-url https://mirrors.aliyun.com/pypi/simple/
# 安装 vLLM 和评测框架，使用清华源
pip install vllm lm-eval transformers==4.41.2 tokenizers==0.19.1 -i https://pypi.tuna.tsinghua.edu.cn/simple

# 5. 安装额外的依赖
echo "📦 正在补全评测脚本所需的其他依赖..."
pip install accelerate datasets pandas -i https://pypi.tuna.tsinghua.edu.cn/simple

# 6. 一次性解决 CRLF 换行符毒瘤
echo "🔧 正在自动修复所有 .sh 脚本的换行符..."
# 这个命令会在云端自动将 Windows 风格换行符转成 Linux 风格，彻底根治 "command not found"
find src/evaluation -type f -name "*.sh" -exec sed -i 's/\r$//' {} +

echo ""
echo "=========================================================="
echo "  🎉 环境已完美搭建完成！"
echo "=========================================================="
echo "👇 请复制并在终端执行下面这条命令，激活你的新环境："
echo "  conda activate medgpt_eval"
echo ""
echo "然后，你就可以畅快地跑评测了，比如："
echo "  bash src/evaluation/run_eval_public.sh sft"
echo "=========================================================="
