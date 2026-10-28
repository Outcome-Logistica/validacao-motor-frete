# syntax=docker/dockerfile:1
# Mesmo padrão do recomendation_container: uv, build em camada cacheável,
# usuário sem privilégio.

ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION}-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependências primeiro — a camada só invalida quando o pyproject/lock muda.
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-editable --no-dev || uv sync --no-editable --no-dev --no-frozen

COPY api.py ./api.py
COPY src ./src

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8003

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8003/health',timeout=4).status==200 else 1)"

CMD ["uv", "run", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8003"]
