# Runs pytest and the eval runner inside the fixture network (SPEC-03 §8.1).
# Python 3.11 to match the production Dockerfile.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && pip install -r /tmp/requirements.txt

# backend/ is bind-mounted at /app by compose, so code edits need no rebuild.
ENTRYPOINT ["sh", "/app/evals/fixture/runner/entrypoint.sh"]
