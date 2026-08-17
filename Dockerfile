FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 安装 Node.js 与 tini（回收孤儿/僵尸进程，不重启 API）
RUN apt-get update && \
    apt-get install -y curl tini && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制依赖文件
COPY requirements.txt .

# 使用 uv 安装基础依赖到系统环境
RUN uv pip install --system -r requirements.txt

# 复制应用代码和启动脚本
COPY app/ ./app/
COPY start.sh .

# 创建依赖目录
RUN mkdir -p /dependencies

# 设置启动脚本权限
RUN chmod +x start.sh

# 暴露端口
EXPOSE 8194

# tini 作为 PID 1，仅负责信号转发与僵尸回收；uvicorn 保持单进程常驻
ENTRYPOINT ["tini", "--"]
CMD ["./start.sh"]