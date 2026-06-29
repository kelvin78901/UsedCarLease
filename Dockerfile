FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    ALR_DATA_DIR=/app/data \
    ALR_DB_PATH=/app/data/autoleaserank.duckdb \
    ALR_LTR_MODEL=/app/data/ltr_lambdamart.txt

WORKDIR /app

# libgomp1 is required by LightGBM
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY alr ./alr
COPY scripts ./scripts
COPY web ./web
RUN pip install --no-cache-dir -e . && mkdir -p /app/data

COPY docker-entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/entrypoint

EXPOSE 8000
ENTRYPOINT ["entrypoint"]
CMD ["uvicorn", "alr.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
