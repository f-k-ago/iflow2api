# GHCR 镜像部署

如果你使用 `ghcr.io/f-k-ago/iflow2api` 镜像，可以直接通过 Docker Compose 或 `docker run` 部署。

## 登录 GHCR

如果仓库或镜像是私有的，先执行：

```bash
echo "YOUR_GITHUB_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

需要的最小权限：`read:packages`

## 使用 Docker Compose

```bash
git clone https://github.com/f-k-ago/iflow2api.git
cd iflow2api

docker compose up -d
```

## 使用 docker run

```bash
docker run -d \
  --name iflow2api \
  -p 28000:28000 \
  -v "$(pwd)/data/iflow2api:/home/appuser/.iflow2api" \
  --restart unless-stopped \
  ghcr.io/f-k-ago/iflow2api:edge
```

## 首次登录

- 管理界面：`http://localhost:28000/admin`
- 默认管理员：`admin / admin`

配置会持久化在：`./data/iflow2api`

## 更新

```bash
docker pull ghcr.io/f-k-ago/iflow2api:edge
docker compose up -d
```
