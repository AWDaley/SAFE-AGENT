# Build only from the allowlisted context produced by episode_service_container.py.
# Official Python manifest resolved and byte-digest verified on 2026-10-20.
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

COPY requirements.lock /opt/episode/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /opt/episode/requirements.lock \
    && python -m pip check
COPY safe_agent /usr/local/lib/python3.12/site-packages/safe_agent
COPY probe.py /opt/episode/probe.py
COPY sources.json /opt/episode/sources.json
RUN python -I -B -c "import safe_agent.inference_service.episode_service; import safe_agent.inference_service.providers.openai_episode; import importlib.util; assert importlib.util.find_spec('tau2') is None"

USER 65532:65532
WORKDIR /
ENTRYPOINT ["python", "-I", "-B", "-m", "safe_agent.inference_service.episode_service"]
