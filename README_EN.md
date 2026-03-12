# iflow2api

English | [简体中文](README.md)

`iflow2api` exposes iFlow accounts as an OpenAI-compatible API and provides a remote Web admin panel.

This repository is now Docker-only. The old GUI, local CLI installation flow, and iFlow CLI import entry have been removed.

## Features

- OpenAI-compatible APIs: `/v1/models`, `/v1/chat/completions`
- Anthropic-compatible API: `/v1/messages`
- Web admin panel: `/admin`
- Upstream account pool with `API Key`, `OAuth`, and `Cookie` login
- Per-account concurrency limit with multi-account routing
- Persistent OAuth / Cookie login data and automatic refresh
- Persistent Docker data directory: `./data/iflow2api`

## Quick Start

### 1. Start the container

```bash
git clone https://github.com/f-k-ago/iflow2api.git
cd iflow2api

docker compose up -d
```

### 2. Open the admin panel

- URL: `http://localhost:28000/admin`
- Default username: `admin`
- Default password: `admin`

Change the admin password after the first login.

### 3. Add upstream accounts

From the `Settings` page, add accounts with one of these methods:

- `API Key`
- `OAuth Login`
- `Cookie Login`

All configuration is stored in `./data/iflow2api` and survives container recreation.

### 4. Use the API

- OpenAI Base URL: `http://localhost:28000/v1`
- Models endpoint: `http://localhost:28000/v1/models`
- Swagger UI: `http://localhost:28000/docs`

Example:

```bash
curl http://localhost:28000/v1/models
```

## Persistent Data

`docker-compose.yml` mounts a single directory:

```text
./data/iflow2api -> /home/appuser/.iflow2api
```

It contains:

- WebUI settings
- Upstream account pool
- Admin users
- JWT secret
- Logs

## Update

```bash
docker compose pull
docker compose up -d
```

## Common Commands

```bash
# Follow logs
docker compose logs -f

# Stop the service
docker compose down

# Force recreate
docker compose up -d --force-recreate
```

## Endpoints

| Path | Description |
| --- | --- |
| `/health` | Health check |
| `/v1/models` | Model list |
| `/v1/chat/completions` | OpenAI Chat Completions |
| `/v1/messages` | Anthropic Messages |
| `/docs` | Swagger UI |
| `/redoc` | ReDoc |
| `/admin` | Web admin panel |
