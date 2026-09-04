"""Shared shape for SaaS collectors.

A real SDK client (slack_sdk.WebClient, a google-api-python-client service,
notion_client.Client, ...) is injected at construction so concrete
collectors stay swappable/testable without changing this interface.
"""

from abc import ABC
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from knowledge_forge.collectors.base import Collector

T = TypeVar("T")


class SaaSAuthConfig(BaseModel):
    """Placeholder auth shape for future real SaaS credential wiring."""

    token: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class SaaSCollector(Collector, ABC):
    def __init__(self, client: Any, auth_config: SaaSAuthConfig | None = None):
        self._client = client
        self._auth_config = auth_config or SaaSAuthConfig()

    @staticmethod
    def _paginate(
        request_fn: Callable[[str | None], tuple[list[T], str | None]]
    ) -> Iterator[T]:
        """Generic cursor-based pagination: calls `request_fn(cursor)` -> (items, next_cursor)
        until `next_cursor` is falsy. Concrete collectors supply `request_fn` bound to
        their specific API call.
        """
        cursor: str | None = None
        while True:
            items, next_cursor = request_fn(cursor)
            yield from items
            if not next_cursor:
                break
            cursor = next_cursor
