"""Base agent class. Every persona inherits from this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from pydantic import BaseModel

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class Agent(ABC, Generic[InputT, OutputT]):
    """Abstract base for the four persona agents.

    Implementations must:
    1. Validate inputs via the typed input model.
    2. Route all LLM calls through `app.services.llm.LLMGateway`.
    3. Emit `agent.started` / `agent.token` / `agent.completed` events.
    4. Write to `audit_log` before returning.
    """

    name: str = "agent"

    @abstractmethod
    async def run(self, payload: InputT) -> OutputT:
        ...
