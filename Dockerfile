FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt pyproject.toml README.md ./
COPY brucebet ./brucebet
COPY configs ./configs
COPY examples ./examples
COPY data ./data

RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -e .

CMD ["python", "-m", "brucebet.telegram_app"]
