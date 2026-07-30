#!/bin/bash
set -e

export HF_ENDPOINT="https://hf-mirror.com"

# 1. Miniconda Check
if [ -d "$HOME/miniconda3" ]; then
    export PATH="$HOME/miniconda3/bin:$PATH"
    source $HOME/miniconda3/etc/profile.d/conda.sh
else
    wget -q https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O miniconda.sh
    bash miniconda.sh -b -p $HOME/miniconda3 -u
    rm miniconda.sh
    export PATH="$HOME/miniconda3/bin:$PATH"
    source $HOME/miniconda3/etc/profile.d/conda.sh
fi

echo "配置 Conda 源 (彻底剥离官方慢速源)..."
conda config --set auto_update_conda false || true
conda clean -i -y

# 2. 创建环境 (直接 clone base 环境，避免任何网络请求！！！)
echo "正在 WSL 中克隆本地环境以避免网络卡顿..."
conda remove -n transformers --all -y || true
conda create -n transformers --clone base -y
conda activate transformers

# 3. 安装依赖 (全走 pip 清华源)
echo "正在安装 vLLM 和依赖..."
pip install torch==2.3.1 torchvision torchaudio -i https://pypi.tuna.tsinghua.edu.cn/simple
pip install vllm lm-eval transformers==4.41.2 tokenizers==0.19.1 -i https://pypi.tuna.tsinghua.edu.cn/simple
pip install conda-pack -i https://pypi.tuna.tsinghua.edu.cn/simple

# 4. 本地测试 vLLM
echo "环境安装完毕！开始下载并测试 0.5B 模型..."
cat << 'EOF' > test_vllm.py
from vllm import LLM, SamplingParams
import shutil
import os

print("====== 开始初始化 vLLM 引擎 ======")
try:
    llm = LLM(model="Qwen/Qwen1.5-0.5B-Chat", trust_remote_code=True, tensor_parallel_size=1, enforce_eager=True)
    sampling_params = SamplingParams(max_tokens=32, temperature=0.1)
    outputs = llm.generate(["请直接回答：1+1等于几？"], sampling_params)
    print("\n✅ vLLM 成功输出结果: ", outputs[0].outputs[0].text)
except Exception as e:
    print(f"\n⚠️ 测试失败，通常是 WSL2 的 CUDA 透传问题: {e}")

try:
    shutil.rmtree(os.path.expanduser("~/.cache/huggingface/hub"), ignore_errors=True)
except:
    pass
EOF

python test_vllm.py
rm test_vllm.py

# 5. 打包环境
echo "====== 正在压缩环境 ======"
conda pack -n transformers -o transformers_env.tar.gz --ignore-editable-packages
echo "✅ 搞定！环境已完美导出到 transformers_env.tar.gz"
