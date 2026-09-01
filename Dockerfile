# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# Official uv static binary - avoids installing uv via pip inside the image.
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /usr/local/bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# Dependencies first, so editing source code doesn't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --extra xlsx --extra pptx

COPY . .
RUN uv sync --frozen --extra xlsx --extra pptx

ENTRYPOINT ["knowledge-forge"]
CMD ["--help"]
