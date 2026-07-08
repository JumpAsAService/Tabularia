FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

# Niente wildcard: se uv.lock manca, fallisce qui (chiaro) e non dopo (confuso)
COPY pyproject.toml uv.lock ./

RUN uv export --frozen | uv pip install --system --no-cache -r -

COPY . .

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
