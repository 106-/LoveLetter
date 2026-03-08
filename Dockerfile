FROM node:20-alpine AS frontend-builder

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci

COPY frontend/ ./frontend/
RUN cd frontend && npm run build


FROM python:3.11-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY loveletter/ ./loveletter/
COPY --from=frontend-builder /app/static ./static

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "loveletter.main:app", "--host", "0.0.0.0", "--port", "8000"]
