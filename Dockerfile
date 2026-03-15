# iFlow2API Dockerfile
# 多阶段构建，优化镜像大小

# 阶段1: 构建阶段
FROM python:3.12-slim AS builder

# 安装构���依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv 包管理器
RUN pip install uv

# 设置工作目录
WORKDIR /app

# 复制依赖文件和 README（pyproject.toml 需要）
COPY pyproject.toml uv.lock README.md ./

# 使用 uv 安装依赖到虚拟环境
RUN uv venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# 使用 uv sync 从 lock 文件安装依赖，--active 使用已存在的虚拟环境
RUN uv sync --frozen --no-dev --active

# 阶段1b: Node bridge 依赖
FROM node:24.14.0-slim AS node-deps
WORKDIR /node-bridge
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

# 阶段2: 运行阶段
FROM python:3.12-slim

# 从官方 Node 运行时复制 node 二进制，供 upstream node_fetch bridge 使用
COPY --from=node:24.14.0-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node-deps /node-bridge/node_modules /app/node_modules
COPY --from=node-deps /node-bridge/package.json /app/package.json

# 安装运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tzdata \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo Asia/Shanghai > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 从构建阶段复制虚拟环境
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 创建非 root 用户
RUN useradd --create-home --shell /bin/bash appuser

# 设置工作目录
WORKDIR /app

# 复制应用代码
COPY --chown=appuser:appuser . .

# 设置环境变量
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV HOME=/home/appuser
ENV TZ=Asia/Shanghai

# 暴露端口
EXPOSE 28000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:28000/health || exit 1

# 启动命令
CMD ["python", "-m", "iflow2api"]
