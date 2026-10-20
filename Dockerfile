FROM node:24-alpine AS frontend
WORKDIR /web
COPY package.json package-lock.json tsconfig.json vite.config.ts ./
COPY frontend ./frontend
RUN npm ci && npm run build

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY tg_archiver ./tg_archiver
COPY --from=frontend /web/tg_archiver/static ./tg_archiver/static
RUN pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1
CMD ["tg-archiver", "run", "--config", "/app/config.toml"]
