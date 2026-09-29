FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    CSMS_DB_PATH=/data/csms.db CSMS_PORT=9000

WORKDIR /app
COPY pyproject.toml ./
COPY csms ./csms
RUN pip install --no-cache-dir . \
    && useradd --uid 568 --create-home csms \
    && mkdir -p /data && chown csms /data

# 568 is TrueNAS's default "apps" user, so the data dataset permissions line up.
USER 568
VOLUME /data
EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/healthz', timeout=4)"

CMD ["csms", "serve"]
