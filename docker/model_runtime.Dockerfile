# Offline protocol worker. Example-only base; pin and audit BASE_IMAGE by digest
# before a production-like build. The host transport itself REQUIRES the
# resulting local image's full sha256 image ID; no mutable runtime tags accepted.
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

COPY src/safe_agent/model_runtime/offline_worker.py /opt/safe_model/offline_worker.py
USER 65534:65534
WORKDIR /
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "-I", "-S", "/opt/safe_model/offline_worker.py"]
