FROM python:3.13.13-slim-bookworm@sha256:355bfa66770995d7e9a0da4b3473b44d0cb451f6b56f5615ad9c39e3c4eca03f AS python-base

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY backend/requirements.txt backend/requirements.txt
RUN python -m pip install --disable-pip-version-check --no-cache-dir -r backend/requirements.txt

FROM python-base AS test

COPY backend/requirements-dev.txt backend/requirements-dev.txt
RUN python -m pip install --disable-pip-version-check --no-cache-dir -r backend/requirements-dev.txt
COPY backend/*.py backend/schema.sql backend/pyproject.toml backend/
COPY backend/tests/ backend/tests/
COPY knowledge/ knowledge/
CMD ["python", "-m", "pytest", "-q", "backend/tests"]

FROM node:24.22.2@sha256:e7add2a490bb17d97b485b8641bd88455fff7839ade0f5eba493c3508124d413 AS frontend

WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run generate

FROM python-base AS app

COPY backend/*.py backend/schema.sql backend/
COPY --from=frontend /src/.output/public/ /app/public/
EXPOSE 8000
USER 10001:10001
CMD ["python", "-m", "uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]
