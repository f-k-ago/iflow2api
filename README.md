# iflow2api

[English Documentation](README_EN.md) | 简体中文

`iflow2api` 将 iFlow 账号服务暴露为 OpenAI 兼容 API，并提供可远程访问的 Web 管理界面。

当前仓库只保留 Docker 部署方式。GUI、本地命令行安装、iFlow CLI 导入等旧入口已移除。

## 功能

- OpenAI 兼容接口：`/v1/models`、`/v1/chat/completions`
- Anthropic 兼容接口：`/v1/messages`
- WebUI 管理界面：`/admin`
- 上游账号池：支持 `API Key`、`OAuth`、`Cookie` 三种登录方式
- 单账号 1 并发限制，多账号自动分流
- OAuth / Cookie 登录数据持久化与自动刷新
- Docker 数据目录持久化：`./data/iflow2api`

## 快速开始

### 1. 启动容器

```bash
git clone https://github.com/f-k-ago/iflow2api.git
cd iflow2api

docker compose up -d
```

### 2. 登录管理界面

- 地址：`http://localhost:28000/admin`
- 默认账号：`admin`
- 默认密码：`admin`

首次登录后建议立即修改管理员密码。

### 3. 配置上游账号

进入 `设置` 页面后，可任选以下方式添加账号：

- `API Key` 账号
- `OAuth 登录`
- `Cookie 登录`

配置会保存到容器挂载目录 `./data/iflow2api`，重建容器后仍然保留。

### 4. 使用 API

- OpenAI Base URL：`http://localhost:28000/v1`
- 模型列表：`http://localhost:28000/v1/models`
- Swagger：`http://localhost:28000/docs`

示例：

```bash
curl http://localhost:28000/v1/models
```

## 持久化目录

`docker-compose.yml` 当前只挂载一个目录：

```text
./data/iflow2api -> /home/appuser/.iflow2api
```

其中包含：

- WebUI 设置
- 上游账号池
- 管理员用户
- JWT 密钥
- 日志文件

## 更新服务

```bash
docker compose pull
docker compose up -d
```

## 常用命令

```bash
# 查看日志
docker compose logs -f

# 停止服务
docker compose down

# 强制重建
docker compose up -d --force-recreate
```

## 兼容接口

| 路径 | 说明 |
| --- | --- |
| `/health` | 健康检查 |
| `/v1/models` | 模型列表 |
| `/v1/chat/completions` | OpenAI Chat Completions |
| `/v1/messages` | Anthropic Messages |
| `/docs` | Swagger UI |
| `/redoc` | ReDoc |
| `/admin` | Web 管理界面 |

## 说明

- 当前推荐并默认支持的部署方式只有 Docker。
- 如果没有配置上游账号，服务仍可启动，但实际请求前需要先在 `/admin` 完成登录。
