FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FAMILYOS_DB=/data/familyos.db
WORKDIR /app

COPY familyos ./familyos
COPY migrations ./migrations

RUN pip install --no-cache-dir websocket-client==1.9.2 \
    && addgroup --system familyos && adduser --system --ingroup familyos familyos \
    && chmod -R a+rX /app \
    && mkdir -p /data && chown familyos:familyos /data

USER familyos
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-m", "familyos.healthcheck"]
CMD ["python", "-m", "familyos"]

