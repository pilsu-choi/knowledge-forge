# syntax=docker/dockerfile:1

# Builds the React+Vite OKF viewer (frontend/) separately so the final image
# doesn't need a Node toolchain - api/app.py mounts /ui from frontend/dist,
# which isn't committed (see frontend/dist's own .gitignore rule: `dist/`).
FROM node:20-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS base

# Official uv static binary - avoids installing uv via pip inside the image.
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /usr/local/bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# Dependencies first, so editing source code doesn't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --extra xlsx --extra pptx --extra hwpx

COPY . .
COPY --from=frontend-build /app/frontend/dist ./frontend/dist
RUN uv sync --frozen --extra xlsx --extra pptx --extra hwpx

ENTRYPOINT ["knowledge-forge"]
CMD ["--help"]
