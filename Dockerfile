FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY tg_archiver ./tg_archiver
RUN pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1
CMD ["tg-archiver", "run", "--config", "/app/config.toml"]
