FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml README.md ./
COPY brucebet ./brucebet
COPY configs ./configs
COPY examples ./examples
COPY data ./data

RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -e .

CMD ["python", "-m", "brucebet.telegram_app"]
