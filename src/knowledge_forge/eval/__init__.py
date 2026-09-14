"""검색 평가 - grain 고정 골든셋 생성(`generate`), 실행(`run`), 비교(`compare`).

설계와 각 게이트가 막는 과거 실패는 wiki/evaluation.md. DB 테이블은 없다: 골든셋은
`tests/eval/golden/<version>.json`, 실행 결과는 `out/eval/<run_id>.json` 파일이다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterable
from typing import TypeVar

T = TypeVar("T")

DEFAULT_PROVIDER = "openai_compatible"
DEFAULT_MODEL = "openai/gpt-4o-mini"


async def gather_bounded(awaitables: Iterable[Awaitable[T]], limit: int) -> list[T]:
    """`asyncio.gather`와 같되 동시에 `limit`개까지만 - 결과는 입력 순서."""
    semaphore = asyncio.Semaphore(max(1, limit))

    async def _one(awaitable: Awaitable[T]) -> T:
        async with semaphore:
            return await awaitable

    return await asyncio.gather(*(_one(a) for a in awaitables))
