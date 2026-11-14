"""google-genai クライアントと、構造化出力（JSON）の呼び出しヘルパー。"""

from __future__ import annotations

import functools
import logging
from typing import TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from app import config

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


@functools.cache
def client(location: str | None = None) -> genai.Client:
    return genai.Client(
        enterprise=True,
        project=config.PROJECT or None,
        location=location or config.LOCATION,
        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=3)),
    )


def thinking(level: str) -> types.ThinkingConfig | None:
    level = (level or "").upper()
    if not level or level == "NONE":
        return None
    return types.ThinkingConfig(thinking_level=level)


async def generate_json(
    *,
    model: str,
    prompt: str | list[types.Part],
    schema: type[T],
    system: str | None = None,
    thinking_level: str = "LOW",
    temperature: float | None = None,
) -> T:
    contents = prompt if isinstance(prompt, list) else [types.Part(text=prompt)]
    response = await client().aio.models.generate_content(
        model=model,
        contents=types.Content(role="user", parts=contents),
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            thinking_config=thinking(thinking_level),
            temperature=temperature,
        ),
    )
    if response.parsed is not None:
        return response.parsed  # type: ignore[return-value]
    return schema.model_validate_json(response.text or "{}")


async def generate_text(
    *,
    model: str,
    prompt: str | list[types.Part],
    system: str | None = None,
    thinking_level: str = "LOW",
) -> str:
    contents = prompt if isinstance(prompt, list) else [types.Part(text=prompt)]
    response = await client().aio.models.generate_content(
        model=model,
        contents=types.Content(role="user", parts=contents),
        config=types.GenerateContentConfig(
            system_instruction=system, thinking_config=thinking(thinking_level)
        ),
    )
    return (response.text or "").strip()
