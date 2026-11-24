FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py geometry.py credit_store.py ./
COPY templates/ templates/
COPY static/ static/

ENV CREDIT_STORE_DIR=/app/data \
    CREDIT_STORE_MAX_BYTES=16777216 \
    CREDIT_STORE_TTL_SECONDS=28800
RUN mkdir -p /app/data && useradd --system --uid 10001 appuser \
    && chown -R appuser:appuser /app/data
USER appuser

EXPOSE 8000
CMD ["python", "app.py"]
