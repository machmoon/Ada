# Cloud Run container. Slim base: the engine is pure Python plus OR-Tools.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

# Dependencies first, so a source edit does not invalidate the wheel layer.
COPY pyproject.toml README.md ./
COPY engine/ ./engine/
RUN pip install --no-cache-dir -e ".[agents,cloud]"

COPY service/ ./service/

# Cloud Run sends SIGTERM and routes to $PORT.
EXPOSE 8080
CMD ["python", "-m", "service.app"]
