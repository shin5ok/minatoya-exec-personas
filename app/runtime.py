"""プロセス内で共有する、実行中の会話の一時情報。

- DIRECTIVES: 進行役が決めた「いまの番」のメモ。(invocation_id, persona_id) をキーにする
  （1回の呼び出しの中だけで使うので、セッションの state には保存しない）
- INTERRUPTS: ユーザーが割り込んだことを、進行中のラウンドに知らせる
"""

from __future__ import annotations

import asyncio
from typing import Any

DIRECTIVES: dict[tuple[str, str], dict[str, Any]] = {}
INTERRUPTS: dict[str, asyncio.Event] = {}


def interrupt_event(session_id: str) -> asyncio.Event:
    ev = INTERRUPTS.get(session_id)
    if ev is None:
        ev = INTERRUPTS[session_id] = asyncio.Event()
    return ev
