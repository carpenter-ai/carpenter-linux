FROM python:3.11-slim-bookworm

WORKDIR /app

# Build deps: C compiler for cryptography, git for pip to fetch carpenter-ai
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential libffi-dev git && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY carpenter_linux/ carpenter_linux/

# The python:3.11 images ship pip 24.0, whose vendored certifi still trusts
# the compromised "emSign ECC Root CA - G3". Upgrade pip and certifi first.
RUN pip install --no-cache-dir --upgrade 'pip>=26.2.2' 'certifi>=2026.10.19'

RUN pip install --no-cache-dir .

# Create runtime directories (must match paths in docker/config.yaml)
RUN mkdir -p /carpenter/data/logs \
             /carpenter/data/code \
             /carpenter/data/workspaces \
             /carpenter/config/kb \
             /carpenter/config/templates \
             /carpenter/config/tools \
             /carpenter/config/skills \
             /carpenter/config/prompts \
             /carpenter/data_models

ENV CARPENTER_CONFIG=/carpenter/config/config.yaml
EXPOSE 7842

CMD ["python3", "-m", "carpenter_linux", "--host", "0.0.0.0", "--port", "7842"]
