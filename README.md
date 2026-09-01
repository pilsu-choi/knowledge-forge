# Knowledge Forge

Enterprise Knowledge Extraction Pipeline: Collector -> Parser -> Adaptive Chunker -> Knowledge Extraction -> Knowledge Object.

See [`../PRD.md`](../PRD.md) for the product requirements and [`wiki/`](wiki/) for architecture, schema, and design-decision documentation.

## Quickstart

```bash
uv sync
uv run knowledge-forge extract path/to/file.md --pretty
```

Requires Anthropic credentials resolvable by the SDK (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or an `ant auth login` profile).
