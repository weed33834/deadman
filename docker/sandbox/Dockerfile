# deadman 沙箱绘图镜像 —— 供 execute_code 图表能力（DockerSandbox 使用）
#
# 用法：docker build -t deadman-sandbox:plot -f docker/sandbox/Dockerfile .
# 配置：SANDBOX_IMAGE=deadman-sandbox:plot
#
# 安全设计（与 deadman 沙箱策略一致）：
#   - 无 GUI：matplotlib 使用 Agg 后端（无 X11/窗口）
#   - 精简依赖：仅常用计算/绘图库，不装网络工具
#   - 由容器运行时 --network=none --memory --cpus 约束（见 deadman.sandbox.DockerSandbox）

FROM python:3.12-slim

# 语言与时区（中文图表标题避免方块）
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg MPLCONFIGDIR=/tmp/matplotlib

# 绘图与数据计算依赖
# numpy/scipy/pandas：数据处理；matplotlib：绘图；Pillow：图像基础
RUN pip install --no-cache-dir \
        matplotlib>=3.7 \
        numpy>=1.24 \
        pandas>=2.0 \
        scipy>=1.10 \
        Pillow>=10.0

# 中文字体：仅当系统提供时启用（可选，安装字体包可让 matplotlib 显示中文）
# RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
#     && rm -rf /var/lib/apt/lists/*

# 沙箱工作目录（可写）
WORKDIR /tmp/deadman-sandbox

# 仅运行用户代码，无额外服务
CMD ["python3", "-c", "print('deadman sandbox plot ready')"]
