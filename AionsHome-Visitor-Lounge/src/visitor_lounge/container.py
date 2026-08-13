"""Dependency assembly boundaries for the independent Visitor Lounge apps."""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
import os
from typing import Any

from .database import Database, utc_now
from .models import GenerationChunk, GenerationRequest
from .settings import Settings


class RequestScopedCodexAdapter:
    """Create and fully close one real Codex adapter per generation request."""

    def __init__(
        self,
        settings: Settings,
        *,
        model: str,
        factory: Callable[..., Any],
    ) -> None:
        self.settings = settings
        self.model = model
        self._factory = factory

    async def generate(
        self, request: GenerationRequest | str
    ) -> AsyncIterator[GenerationChunk]:
        prompt = request.prompt if isinstance(request, GenerationRequest) else request
        adapter = self._factory(self.settings, model=self.model)
        stream = adapter.generate(prompt)
        try:
            async for chunk in stream:
                yield chunk
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()


async def _generate_summary(
    adapter: RequestScopedCodexAdapter, prompt: str
) -> tuple[str, dict[str, int]]:
    visible: list[str] = []
    usage: dict[str, int] = {}
    async for chunk in adapter.generate(prompt):
        if chunk.kind == "text" and chunk.text:
            visible.append(chunk.text)
        elif chunk.kind == "usage":
            usage = {key: int(value) for key, value in chunk.usage.items()}
    return "".join(visible), usage


@dataclass
class Container:
    settings: Settings
    database: Database
    codex_adapter: Any | None = None
    clock: Callable[[], datetime] = utc_now
    resource_sampler: Callable[[], Any] | None = None
    visitor_service: Any | None = None
    turn_coordinator: Any | None = None
    background: Any | None = None

    @classmethod
    def build(cls, settings: Settings) -> "Container":
        return cls(settings=settings, database=Database(settings.database_path))

    @classmethod
    def build_admin(cls, settings: Settings) -> "Container":
        """Assemble only the database and management dependencies."""
        return cls.build(settings)

    @classmethod
    def build_visitor(
        cls,
        settings: Settings,
        *,
        model: str | None = None,
        codex_adapter_factory: Callable[..., Any] | None = None,
    ) -> "Container":
        """Assemble the one generation slot and its low-priority coordinator."""
        from .background import BackgroundCoordinator
        from .codex_adapter import CodexAdapter
        from .visitor_service import VisitorService

        adapter = RequestScopedCodexAdapter(
            settings,
            model=model or os.environ.get(
                "VISITOR_LOUNGE_CODEX_MODEL", "gpt-5.6-terra"
            ),
            factory=codex_adapter_factory or CodexAdapter,
        )
        container = cls(
            settings=settings,
            database=Database(settings.database_path),
            codex_adapter=adapter,
        )
        service = VisitorService(container)
        background = BackgroundCoordinator(
            database=container.database,
            prompt_builder=service.prompt_builder,
            scheduler=service.scheduler,
            summary_generator=lambda _job, prompt: _generate_summary(adapter, prompt),
            clock=container.clock,
        )
        container.visitor_service = service
        container.background = background
        return container

    def startup_recovery(self) -> int:
        self.database.initialize()
        return self.database.recover_stale_jobs(self.clock())
