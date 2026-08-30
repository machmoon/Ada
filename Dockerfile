# Review UI bundle. Built here so the runtime image needs no Node at all.
FROM node:22-slim AS frontend

WORKDIR /frontend

# Manifest first, so a source edit does not invalidate the npm install layer.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

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

# service/app.py serves this at / -- same origin as /generate, so no CORS.
COPY --from=frontend /frontend/dist ./frontend/dist

# Cloud Run sends SIGTERM and routes to $PORT.
EXPOSE 8080
CMD ["python", "-m", "service.app"]
