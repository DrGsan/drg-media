FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FP_DATA_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 archive && useradd -u 10001 -g archive -m archive \
    && mkdir /data && chown archive:archive /data
WORKDIR /app
COPY pyproject.toml requirements.lock.txt LICENSE ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY family_photo ./family_photo
COPY licenses ./licenses
COPY THIRD_PARTY_NOTICES.md dependency-licenses.json ./
RUN pip install --no-cache-dir --no-deps .
USER archive
EXPOSE 8090
CMD ["gunicorn", "--bind", "0.0.0.0:8090", "--workers", "1", "--threads", "8", "--timeout", "120", "--keep-alive", "30", "family_photo.api:create_app()"]
