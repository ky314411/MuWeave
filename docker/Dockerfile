ARG NODE_IMAGE=node:24-bookworm-slim
FROM ${NODE_IMAGE} AS web-dev
WORKDIR /app/frontend
RUN chown node:node /app/frontend
USER node
COPY --chown=node:node frontend/package*.json ./
RUN --mount=type=cache,target=/home/node/.npm,uid=1000,gid=1000 npm ci
COPY --chown=node:node frontend/ ./
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

FROM web-dev AS web-build
RUN npm run build

FROM python:3.12-slim-bookworm AS python-base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" MUWEAVE_DATA_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg fonts-noto-cjk ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 muweave \
    && useradd --uid 10001 --gid muweave --create-home muweave \
    && mkdir -p /app /data /run/muweave-secrets /models \
    && chown -R muweave:muweave /app /data /run/muweave-secrets /models
RUN python -m pip install --no-cache-dir uv==0.8.17
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
ARG INSTALL_TRANSCRIPTION=false
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$INSTALL_TRANSCRIPTION" = true ]; then uv sync --locked --no-dev --extra transcription; \
    else uv sync --locked --no-dev; fi
COPY backend/ ./backend/
COPY scripts/ ./scripts/
USER muweave
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)); assert r['status']=='ok' and r['ffmpeg'] and r['ffprobe']"]
CMD ["uvicorn", "app.main:create_app", "--factory", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]

FROM python-base AS backend-dev
USER root
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$INSTALL_TRANSCRIPTION" = true ]; then uv sync --locked --extra transcription; \
    else uv sync --locked; fi
USER muweave
CMD ["uvicorn", "app.main:create_app", "--factory", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "backend"]

FROM python-base AS production
COPY --from=web-build /app/frontend/dist ./frontend/dist
