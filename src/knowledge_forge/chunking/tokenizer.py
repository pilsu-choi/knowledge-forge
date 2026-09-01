"""Token counting strategy, isolated behind one function.

Uses tiktoken's cl100k_base encoding as a fast, dependency-light local
heuristic. This is OpenAI's tokenizer, not Claude's actual tokenizer, so
counts will drift from what the Anthropic API would report - see
wiki/adaptive-chunking.md for the mitigation (a safety margin applied at
the chunk-boundary decision in chunking.adaptive).
"""

from functools import lru_cache

import tiktoken


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return len(_encoding().encode(text))


def encode(text: str) -> list[int]:
    return _encoding().encode(text)


def decode(tokens: list[int]) -> str:
    return _encoding().decode(tokens)
