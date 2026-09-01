# Knowledge Forge

Enterprise Knowledge Extraction Pipeline: Collector -> Parser -> Adaptive Chunker -> Knowledge Extraction -> Knowledge Object.

See [`../PRD.md`](../PRD.md) for the product requirements and [`wiki/`](wiki/) for architecture, schema, and design-decision documentation.

## Quickstart

```bash
uv sync
uv run knowledge-forge extract path/to/file.md --pretty
```

Requires Anthropic credentials resolvable by the SDK (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or an `ant auth login` profile).

## Docker

```bash
export ANTHROPIC_API_KEY=sk-ant-...
docker compose up -d postgres     # local Postgres for Source/Document/Chunk/KnowledgeObject storage
docker compose run --rm app db init
docker compose run --rm app extract /app/input/sample.md --pretty
docker compose run --rm app schedule /app/input --interval-seconds 3600
```

Put files to ingest under `./input` (mounted read-only into the `app` container at `/app/input`); the raw blob store persists under `./data`. See [`wiki/dev-guide.md`](wiki/dev-guide.md) and [`wiki/storage.md`](wiki/storage.md) for details.
