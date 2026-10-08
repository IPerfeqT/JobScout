FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    JOBTOOL_MODE=demo \
    JOBTOOL_DB_PATH=/data/jobtool.db \
    JOBTOOL_REFRESH_INTERVAL_MIN=180

WORKDIR /app
COPY pyproject.toml README.md ./
COPY jobtool ./jobtool
COPY extensions ./extensions
RUN pip install --no-cache-dir . && useradd --create-home app && mkdir /data && chown app /data

USER app
VOLUME /data
EXPOSE 8000
CMD ["python", "-m", "jobtool", "serve", "--host", "0.0.0.0", "--port", "8000"]
