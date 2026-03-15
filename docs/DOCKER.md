# Docker 部署指南

`iflow2api` 当前只支持 Docker 部署。本指南对应仓库内的 `Dockerfile` 与 `docker-compose.yml`。

## 启动

```bash
git clone https://github.com/f-k-ago/iflow2api.git
cd iflow2api

docker compose up -d
```

镜像现在会在构建阶段内置一份与当前项目对齐版本的官方 `iflow-cli` bundle **及其运行时依赖**，并自动设置：

```text
IFLOW_OFFICIAL_BUNDLE_PATH=/opt/iflow-official/package/bundle/iflow.js
```

因此 Docker 部署默认可直接满足 strict official parity 模式。

## 访问地址

- 管理界面：`http://localhost:28000/admin`
- OpenAI Base URL：`http://localhost:28000/v1`
- Swagger：`http://localhost:28000/docs`
- 健康检查：`http://localhost:28000/health`

## 首次初始化

1. 打开 `http://localhost:28000/admin`
2. 使用默认管理员账号登录：`admin / admin`
3. 在 `设置` 页面添加上游账号
4. 选择 `API Key`、`OAuth` 或 `Cookie` 登录方式之一

## 数据持久化

Compose 默认挂载：

```text
./data/iflow2api -> /home/appuser/.iflow2api
```

该目录会保存：

- WebUI 设置
- 上游账号池
- 管理员用户
- JWT 密钥
- 日志

## 常用命令

```bash
# 查看日志
docker compose logs -f

# 停止
docker compose down

# 拉取新镜像并重启
docker compose pull
docker compose up -d

# 强制重建
docker compose up -d --force-recreate
```

## 健康检查

```bash
curl http://localhost:28000/health
```

## 故障排查

### 管理界面可以打开，但接口不可用

先检查是否已经在 `/admin` 中配置上游账号。

### 配置重建后丢失

确认 `./data/iflow2api` 目录没有被删掉，并且 Compose 中仍然挂载到 `/home/appuser/.iflow2api`。

### 启动时报 `PermissionError: /home/appuser/.iflow2api/logs`

这是宿主机挂载目录权限不足导致的。当前 `docker-compose.yml` 已通过 `root` 用户运行容器并固定 `HOME=/home/appuser` 规避该问题。

如果你之前已经启动过旧版本配置，执行一次重建即可：

```bash
docker compose up -d --force-recreate
```

### 容器启动失败

```bash
docker compose logs --tail=200
```

如果日志里出现：

```text
当前已强制要求 patched 官方 bundle，但未找到可用的 package/bundle/iflow.js
```

说明当前运行环境没有拿到官方 bundle。对 Docker 部署，这通常意味着你还在使用旧镜像；执行一次：

```bash
docker compose pull
docker compose up -d --force-recreate
```

如果是非 Docker 自部署，请显式设置：

```bash
export IFLOW_OFFICIAL_BUNDLE_PATH=/绝对路径/package/bundle/iflow.js
```

### 端口冲突

如果宿主机 `28000` 已被占用，修改 `docker-compose.yml` 的端口映射，例如：

```yaml
ports:
  - "38000:28000"
```

然后通过 `http://localhost:38000` 访问。
