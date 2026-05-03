FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md TOKEN_REFRESH.md /app/
COPY src /app/src

RUN pip install --no-cache-dir ".[refresh]" \
    && python -m playwright install --with-deps chromium

CMD ["copilot-openai-proxy", "serve", "--host", "0.0.0.0", "--port", "8000"]
