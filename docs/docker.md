# Docker 本地运行

在 MuWeave 仓库根目录使用 Docker Desktop 的 Linux 容器模式。依赖和前端均在镜像中构建，宿主机无需 Python、Node 或 FFmpeg。开发、生产配置是两个独立的 Compose 项目，单独使用 `-f`，不要叠加。

## 生产方式

```powershell
docker compose up -d --build --wait
docker compose ps
docker compose logs --tail 100 app
```

访问 http://127.0.0.1:8080。多阶段构建将前端静态文件装入后端镜像，一个非 root 进程提供 UI 和 API；包含 FFmpeg、ffprobe、中文字体。使用 init 转发信号并回收子进程，单个 worker 管理 SQLite 与后台任务。

```powershell
docker compose stop
docker compose start --wait
# 删除容器和网络，保留项目和凭据：
docker compose down
```

这是个人本地应用的生产构建，默认只绑定回环地址。当前没有多用户认证，勿直接暴露到公网。

## 开发方式

```powershell
docker compose -f compose.dev.yaml up -d --build --wait
docker compose -f compose.dev.yaml ps
docker compose -f compose.dev.yaml logs --tail 100 backend frontend
```

访问 http://127.0.0.1:5173，API 为 http://127.0.0.1:8000。前端通过容器网络代理 API 和事件流；Python 源码与前端源码只读挂载，修改宿主机文件后自动刷新。Windows 下启用轮询，避免原生文件监听问题。容器自己的依赖不会被宿主机 `.venv`、`node_modules` 覆盖。修改依赖、构建参数或未挂载的配置后，重新执行上述构建命令。

```powershell
docker compose -f compose.dev.yaml stop
docker compose -f compose.dev.yaml start --wait
docker compose -f compose.dev.yaml down
```

可同时运行两套，数据相互独立：

| 用途 | 生产卷 | 开发卷 | 容器路径 |
| --- | --- | --- | --- |
| 项目、素材、导出、SQLite、加密凭据 | muweave_data | muweave-dev_data | /data |
| 凭据加密主密钥 | muweave_keys | muweave-dev_keys | /run/muweave-secrets |
| 可选模型目录 | muweave_models | muweave-dev_models | /models，只读 |

`down` 和镜像重建会保留卷；`down -v` 会删除卷与数据，不用于日常停止。首次启动创建独立数据，不自动迁移宿主机 `.muweave`。

## 供应商密钥与本机模型

在界面设置页保存 API Key。普通宿主机启动继续使用系统 keyring；Compose 显式设置 `MUWEAVE_MASTER_KEY_FILE`，启用 Fernet 加密凭据文件，主密钥放在另一个卷，文件权限为 0600。密钥不会写入前端响应、SQLite 明文字段或镜像。

这是便于无桌面容器运行的静态加密，拥有两个卷读取权限的人仍可解密。备份、迁移必须同时保留 data 和 keys；主密钥丢失时会报错，不会悄悄生成新密钥覆盖已有凭据。

容器里的 `localhost` 指向容器。访问宿主机 Ollama 时连接地址填写 `http://host.docker.internal:11434/v1`，协议选择 Ollama；宿主机服务必须允许来自 Docker 网络的连接。不要把供应商地址填成 Compose 内部服务名，除非供应商确实在同一网络。

可选转写依赖：复制 `.env.example` 为 `.env`，设 `MUWEAVE_TRANSCRIPTION=true`，再构建。设 `MUWEAVE_MODELS_DIR=D:/models` 可将已有模型父目录只读挂载到 `/models`；界面填写如 `/models/faster-whisper-small`，该目录应包含 `model.bin`。默认不安装转写扩展、不下载模型、不配置 GPU。

端口也可在 `.env` 调整，默认生产 8080、开发前端 5173、开发 API 8000。源站保护根据实际请求地址验证，同源访问不依赖固定生产端口。

若容器直连外网超时，可在本地 `.env` 设置 `MUWEAVE_PROXY=http://host.docker.internal:7890`（使用自己的 HTTP 代理地址），同时用于构建依赖下载和后端访问供应商。默认不启用；不代理本机 Ollama 或容器内部 API。此设置不改变 Docker daemon 拉取基础镜像时的代理；基础镜像拉取仍取决于 Docker Desktop 的网络设置。

前端构建默认使用 Node 24。镜像仓库暂时不可用而本机已有兼容镜像时，可在 `.env` 设置 `MUWEAVE_NODE_IMAGE=node:22-alpine`，需确认缓存版本至少为 22.12；Node 只用于构建和开发服务器，生产运行镜像仍为 Python 3.12。

## 验证与备份

```powershell
docker compose exec app python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health')))"
docker compose -f compose.dev.yaml exec backend python -m pytest -q
docker compose -f compose.dev.yaml exec frontend npm test
docker compose exec app python scripts/smoke_export.py --duration 3 --data-dir /data/technical-smoke
```

最后一条生成合成音视频并烧录字幕，用于验证容器中的 FFmpeg，不是模型生成质量验收。测试内容独立放在 `/data/technical-smoke`。

备份前先停止应用，避免复制到 SQLite 写入中的状态。下面备份生产 data 与 keys 到当前目录的 `backups`；备份包含解密所需的主密钥，需妥善保管。不要提交到 Git。

```powershell
docker compose stop
New-Item -ItemType Directory -Force backups | Out-Null
docker run --rm --user 0 --entrypoint tar -v muweave_data:/data:ro -v muweave_keys:/keys:ro -v "${PWD}/backups:/backup" muweave:local -czf /backup/muweave-backup.tar.gz /data /keys
docker compose start --wait
```

开发环境改用 `muweave-dev_data`、`muweave-dev_keys`，并先停止对应开发服务。已有 faster-whisper 模型目录另外备份。恢复前停止目标服务，将备份中 data 和 keys 分别还原到对应卷，确保归属 UID/GID 10001，再启动；不要覆盖尚未备份的现有卷。

## 本轮验证记录（2026-11-03）

- Docker Desktop Linux 引擎 / Compose 5；开发后端、开发前端、生产应用均完成构建和健康检查。生产 UID/GID 10001，开发前端 UID/GID 1000，均使用 init。
- 容器内后端 44 项、前端 2 项测试通过；Chromium 实际打开生产和开发界面，项目和 Monaco 内容正常加载，未出现页面脚本错误。
- 从两个入口完成同源项目创建、文件变更、供应商凭据保存。Vite 显式保留浏览器 Host，避免字符串代理自动改写 Host 导致后端拒绝；已通过实际 HTTP 请求验证。
- 生产容器完成 3.000 秒合成素材的有声 MP4、烧录字幕、SRT 和清单导出；不是 AI 短剧质量验收。
- 容器主密钥和凭据文件实际加解密验证通过；开发、生产容器重建后的项目文件和凭据读取通过。两个入口均可接收 SSE 保活数据，开发环境实际检测到宿主机文件时间变化并完成后端热重载，前端配置热重载也通过。
- 本机 Docker 外网连接不稳定，实际构建使用已缓存的 Node 22.23.4 和本机现有 HTTP 代理；仅写入忽略的本地 `.env`。仓库默认仍使用 Node 24。后续前端增量构建已命中依赖缓存。
- 可选转写扩展和 GPU 未在本轮构建中启用；没有下载模型或调用付费供应商。保留现有前端依赖锁文件，构建出现既有依赖审计告警；官方 advisory API 确认 Monaco 固定的 DOMPurify 3.2.7 有[已知 XSS 告警](https://github.com/advisories/GHSA-h8r8-wccr-v5f2)，依赖升级另行处理。本轮不代表公网部署或依赖安全审计通过。
