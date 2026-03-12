# 使用 GHCR 私有镜像部署指南

## 🔐 前置要求

由于仓库是私有的，构建的 Docker 镜像也是私有的，需要先进行认证。

### 1. 创建 GitHub Personal Access Token

1. 访问 https://github.com/settings/tokens
2. 点击 **"Generate new token (classic)"**
3. 勾选权限：
   - ✅ `read:packages` - 读取容器镜像
4. 点击 **"Generate token"**
5. **复制并保存** token（只显示一次）

### 2. 登录 GitHub Container Registry

```bash
echo "YOUR_GITHUB_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

**示例：**
```bash
echo "ghp_xxxxxxxxxxxxxxxxxxxx" | docker login ghcr.io -u f-k-ago --password-stdin
```

成功后会显示：
```
Login Succeeded
```

## 🚀 部署方式

### 方式1：使用 docker-compose（推荐）

```bash
# 1. 克隆仓库（或只下载 docker-compose.yml）
git clone https://github.com/f-k-ago/iflow2api.git
cd iflow2api

# 2. 复制环境变量配置
cp .env.example .env

# 3. 编辑 .env 文件，填入你的 API Key
nano .env

# 4. 启动服务
docker-compose up -d

# 5. 查看日志
docker-compose logs -f
```

### 方式2：直接使用 docker run

```bash
docker run -d \
  --name iflow2api \
  -p 28000:28000 \
  -e IFLOW_API_KEY=sk-your-api-key \
  -v ~/.iflow:/home/appuser/.iflow:ro \
  --restart unless-stopped \
  ghcr.io/f-k-ago/iflow2api:edge
```

## 🏷️ 可用的镜像标签

| 标签 | 说明 | 更新时机 |
|------|------|---------|
| `edge` | 最新开发版 | 每次推送到 main 分支 |
| `latest` | 最新稳定版 | 每次发布新版本标签 |
| `1.0.0` | 特定版本 | 固定不变 |

## 🔄 更新镜像

```bash
# 拉取最新镜像
docker-compose pull

# 重启服务
docker-compose up -d
```

## 🛠️ 常见问题

### Q: 提示 "unauthorized: authentication required"

**原因：** 未登录或 token 过期

**解决：**
```bash
# 重新登录
echo "YOUR_GITHUB_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

### Q: 提示 "denied: permission_denied"

**原因：** Token 权限不足

**解决：** 重新创建 token，确保勾选了 `read:packages` 权限

### Q: 在 CI/CD 中使用

在 GitHub Actions 中可以直接使用 `GITHUB_TOKEN`：

```yaml
- name: Login to GHCR
  uses: docker/login-action@v3
  with:
    registry: ghcr.io
    username: ${{ github.actor }}
    password: ${{ secrets.GITHUB_TOKEN }}

- name: Pull image
  run: docker pull ghcr.io/f-k-ago/iflow2api:edge
```

## 📝 配置说明

详见 `.env.example` 文件中的配置项说明。

主要配置：
- `IFLOW_API_KEY` - iFlow API 密钥（必需）
- `ENABLE_CONCURRENCY_LIMIT` - 是否启用并发限制（默认 true）
- `MAX_CONCURRENT_REQUESTS` - 最大并发数（默认 1，遵守官方规则）