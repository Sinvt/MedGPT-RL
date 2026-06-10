# 运行方式: bash run_webui_7b.sh

export CUDA_VISIBLE_DEVICES="0"
export PYTHONPATH=$PWD:$PYTHONPATH

# 启动 Web 界面，加载刚刚合并完成的最终版 PPO 强化学习大模型
# --share 参数会自动生成一个可以在外网直接访问的 gradio 链接
python demo/gradio_demo.py \
    --base_model "merged-ppo-qwen-7b" \
    --port 8081 \
    --share
