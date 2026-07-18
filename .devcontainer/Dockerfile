FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

ENV PATH="/home/vscode/.local/bin:${PATH}"

RUN rm -f /etc/apt/sources.list.d/yarn.list \
    && apt-get update \
    && apt-get install --yes --no-install-recommends fish zsh \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir \
      build==1.2.2.post1 \
      mypy==1.15.0 \
      twine==6.1.0 \
    && mkdir -p /home/vscode/.codex \
    && chown -R vscode:vscode /home/vscode/.codex
