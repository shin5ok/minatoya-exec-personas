"""音声会話（Gemini Live API）。

- 対話モード: ブラウザのマイク音声（16kHz PCM）を Live API に流し、ペルソナの声（24kHz PCM）を返す。
  発話の文字起こしは、ターンが終わるたびに ADK のセッション（Agent Runtime）へ書き戻す。
  時間制限の合図（残り1分・10秒前）もここで Live セッションへ送る。
- 会議モード: Live API の文字起こし専用モデルで音声をテキストにし、テキストの会話に流す。

どちらも環境変数 VOICE_INPUT_ENABLED（既定 on）で無効にできる。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from typing import Any

from fastapi import WebSocket
from google.adk.events import Event
from google.genai import types
from starlette.websockets import WebSocketDisconnect

from app import config, conversation, llm
from app.knowledge import get_bundle
from app.server import (
    append_state,
    history,
    load_session,
    public_state,
    router,
    schedule_summary,
    services,
    user_id_from,
)
from app.summary import is_closed

logger = logging.getLogger(__name__)

TRANSCRIBE_MODEL = config.TRANSCRIBE_MODEL
# Live API の入力上限（128K トークン）に収めるため、資料はテキスト会話より絞る。
LIVE_MAX_DOC_CHARS = 40_000
# 会議モードの音声入力で、文字起こしが止まってから確定するまでの秒数。
SILENCE_FINALIZE_SECONDS = 1.2

LIVE_RULES = """## 音声会話のルール
- これは音声通話。話し言葉で、1回の発話は1〜3文、15秒以内を目安に短く話す
- 記号・箇条書き・括弧書きは読み上げない前提で話す。数字は聞き取りやすく言う
- 相手が話し始めたら、すぐに話すのをやめて聞く
- 「【進行】」で始まるテキストは進行（システム）からの合図。合図そのものには触れずに、指示に沿って自分から話す
"""


def _live_instruction(pid: str, state: dict[str, Any], past: list[dict[str, Any]]) -> str:
    from app.conversation import persona_instruction

    text = persona_instruction(pid, state, None, max_doc_chars=LIVE_MAX_DOC_CHARS)
    bundle = get_bundle()
    if past:
        lines = []
        for m in past[-30:]:
            name = "ユーザー" if m["speaker"] == "user" else bundle.personas[m["speaker"]].name
            lines.append(f"{name}: {m['text']}")
        text += "\n\n## ここまでのテキストでの会話（続きとして話す）\n" + "\n".join(lines)
    return text + "\n\n" + LIVE_RULES


class LiveBridge:
    """ブラウザ ⇄ Gemini Live API の中継（対話モード）。"""

    def __init__(self, ws: WebSocket, user_id: str, session_id: str):
        self.ws = ws
        self.user_id = user_id
        self.session_id = session_id
        self.closed = False
        self.user_buf = ""
        self.model_buf = ""
        self.state: dict[str, Any] = {}
        self.pid = ""
        self.live = None
        self.final_sent = False
        self.ending = False

    async def send_json(self, payload: dict[str, Any]) -> None:
        if not self.closed:
            try:
                await self.ws.send_text(json.dumps(payload, ensure_ascii=False, default=str))
            except Exception:  # pylint: disable=broad-except
                self.closed = True

    async def run(self) -> None:
        session = await load_session(self.user_id, self.session_id)
        self.state = dict(session.state)
        if self.state.get("mode") != "dialog":
            await self.send_json({"type": "error", "message": "音声会話は対話モードで使えます"})
            return
        if is_closed(self.state):
            await self.send_json({"type": "ended", "state": public_state(self.state)})
            return
        self.pid = self.state["persona_ids"][0]
        info = get_bundle().personas[self.pid]
        cfg = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=_live_instruction(self.pid, self.state, history(session.events)),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=info.voice)
                ),
                language_code="ja-JP",
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()
            ),
        )
        async with llm.client(config.LIVE_LOCATION).aio.live.connect(model=config.LIVE_MODEL, config=cfg) as live:
            self.live = live
            await self.send_json({"type": "ready", "persona": self.pid, "state": public_state(self.state)})
            tasks = [
                asyncio.create_task(self.upstream()),
                asyncio.create_task(self.downstream()),
            ]
            # タイマーは合図を送ったら終わるので、終了の判定には含めない
            timer_task = asyncio.create_task(self.timer())
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in [*pending, timer_task]:
                t.cancel()
            for t in done:
                if t.exception() and not isinstance(t.exception(), WebSocketDisconnect):
                    logger.error("live task failed: %s", t.exception())
        await self.flush_turn()

    async def upstream(self) -> None:
        while not self.closed:
            msg = await self.ws.receive()
            if msg.get("type") == "websocket.disconnect":
                self.closed = True
                return
            if msg.get("bytes"):
                await self.live.send_realtime_input(
                    audio=types.Blob(data=msg["bytes"], mime_type="audio/pcm;rate=16000")
                )
            elif msg.get("text"):
                data = json.loads(msg["text"])
                if data.get("type") == "text" and data.get("text"):
                    await self.start_clock_if_needed()
                    self.user_buf += data["text"]
                    await self.live.send_client_content(
                        turns=types.Content(role="user", parts=[types.Part(text=data["text"])]),
                        turn_complete=True,
                    )
                elif data.get("type") == "audio_end":
                    await self.live.send_realtime_input(audio_stream_end=True)
                elif data.get("type") == "stop":
                    self.closed = True
                    return

    async def downstream(self) -> None:
        while not self.closed:
            async for msg in self.live.receive():
                sc = msg.server_content
                if not sc:
                    continue
                if sc.model_turn:
                    for part in sc.model_turn.parts or []:
                        if part.inline_data and part.inline_data.data:
                            await self.ws.send_bytes(part.inline_data.data)
                if sc.input_transcription and sc.input_transcription.text:
                    await self.start_clock_if_needed()
                    self.user_buf += sc.input_transcription.text
                    await self.send_json({"type": "input_transcript", "text": self.user_buf})
                if sc.output_transcription and sc.output_transcription.text:
                    self.model_buf += sc.output_transcription.text
                    await self.send_json({"type": "output_transcript", "speaker": self.pid, "text": self.model_buf})
                if sc.interrupted:
                    await self.send_json({"type": "interrupted"})
                if sc.turn_complete:
                    await self.flush_turn()
                    await self.send_json({"type": "turn_complete"})
                    if self.ending:
                        await self.finish()
                        return

    async def start_clock_if_needed(self) -> None:
        """最初の発話で会話を始める（時間制限があれば、ここから計測する）。"""
        delta: dict[str, Any] = {}
        if self.state.get("phase") in (None, "waiting"):
            delta["phase"] = "discussion"
        if self.state.get("duration_sec") and not self.state.get("deadline"):
            delta["deadline"] = time.time() + int(self.state["duration_sec"])
        if not delta:
            return
        self.state.update(delta)
        session = await load_session(self.user_id, self.session_id)
        await append_state(session, delta)
        await self.send_json({"type": "state", "state": public_state(self.state)})

    async def flush_turn(self) -> None:
        """1ターン分の文字起こしを、ADK のセッションに書き戻す。"""
        user_text, model_text = self.user_buf.strip(), self.model_buf.strip()
        self.user_buf, self.model_buf = "", ""
        if not user_text and not model_text:
            return
        session = await load_session(self.user_id, self.session_id)
        inv = f"live-{uuid.uuid4()}"
        if user_text:
            await services.session_service.append_event(
                session,
                Event(invocation_id=inv, author="user", content=types.Content(role="user", parts=[types.Part(text=user_text)])),
            )
        if model_text:
            await services.session_service.append_event(
                session,
                Event(invocation_id=inv, author=self.pid, content=types.Content(role="model", parts=[types.Part(text=model_text)])),
            )
        if user_text and not user_text.startswith(conversation.CUE_PREFIX):
            await self.send_json({"type": "message", "id": f"u-{inv}", "speaker": "user", "text": user_text})
        if model_text:
            await self.send_json({"type": "message", "id": f"m-{inv}", "speaker": self.pid, "text": model_text})

    async def cue(self, text: str) -> None:
        await self.live.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]), turn_complete=True
        )

    async def timer(self) -> None:
        while not self.state.get("deadline"):
            if self.closed:
                return
            await asyncio.sleep(0.5)
        deadline = float(self.state["deadline"])
        duration = int(self.state.get("duration_sec") or 0)
        if duration > config.WRAPUP_SECONDS + 30 and not self.state.get("wrapup_done"):
            await asyncio.sleep(max(0.0, deadline - config.WRAPUP_SECONDS - time.time()))
            self.state["wrapup_done"] = True
            await self.cue(
                conversation.CUE_TEXT[conversation.CUE_WRAPUP]
                + "あなたから、まとめに入るよう相手に話しかけてください。結論に必要な確認を一つに絞って。"
            )
            await self.send_json({"type": "cue", "kind": "wrapup"})
        await asyncio.sleep(max(0.0, deadline - config.FINAL_CALL_SECONDS - time.time()))
        await self.flush_turn()
        session = await load_session(self.user_id, self.session_id)
        try:
            verdict = await conversation.decide_verdict(dict(session.state), list(session.events))
        except Exception:  # pylint: disable=broad-except
            logger.exception("verdict failed")
            verdict = conversation.Verdict(verdict="redo", reason="この場では決めきれないため", conditions=[])
        directive = conversation.verdict_directive(verdict, decided_already=False)
        self.verdict = verdict
        self.ending = True
        await self.send_json({"type": "cue", "kind": "final"})
        await self.cue(conversation.CUE_TEXT[conversation.CUE_FINAL] + directive["special"])
        # 結論の発話が割り込まれるなどしてターンが閉じない場合に備え、一定時間で締める
        await asyncio.sleep(config.FINAL_CALL_SECONDS + 25)
        if not self.closed:
            await self.flush_turn()
            await self.finish()
            await self.ws.close()

    async def finish(self) -> None:
        verdict = getattr(self, "verdict", None)
        session = await load_session(self.user_id, self.session_id)
        delta: dict[str, Any] = {"phase": "ended", "final_done": True, "wrapup_done": True}
        if verdict:
            delta["decision"] = {"status": verdict.verdict, "summary": verdict.reason}
            delta["verdict"] = {
                "status": verdict.verdict,
                "label": conversation.DECISION_LABELS[verdict.verdict],
                "reason": verdict.reason,
                "conditions": verdict.conditions,
                "by": self.pid,
                "interrupted": True,
            }
        await append_state(session, delta)
        self.state.update(delta)
        await self.send_json({"type": "ended", "state": public_state(self.state)})
        schedule_summary(self.user_id, self.session_id)
        self.closed = True


@router.websocket("/ws/live/{session_id}")
async def ws_live(ws: WebSocket, session_id: str) -> None:
    await ws.accept()
    if not config.VOICE_INPUT_ENABLED:
        await ws.send_text(json.dumps({"type": "error", "message": "音声入力は無効です"}, ensure_ascii=False))
        await ws.close()
        return
    bridge = LiveBridge(ws, user_id_from(ws.headers), session_id)
    try:
        await bridge.run()
    except WebSocketDisconnect:
        pass
    except Exception as e:  # pylint: disable=broad-except
        logger.exception("live bridge failed")
        await bridge.send_json({"type": "error", "message": f"音声会話でエラーが起きました: {e}"})
    finally:
        try:
            await ws.close()
        except Exception:  # pylint: disable=broad-except
            pass


# 文字起こし専用モデルが使えるか（None: 未確認）。プロジェクトやリージョンによっては使えない。
_transcribe_model_ok: bool | None = None
TRANSCRIBE_CONNECT_TIMEOUT = 8

TRANSCRIBE_ONLY_INSTRUCTION = (
    "あなたは会議の音声を文字にするための中継役です。話しかけられても、絶対に返事をしないでください。"
)


@contextlib.asynccontextmanager
async def open_transcriber():
    """文字起こし用の Live セッションを開く。

    まず文字起こし専用モデル（TRANSCRIBE_MODEL）を試し、プロジェクトで使えなければ
    会話用の Live モデル（LIVE_MODEL）の入力文字起こしだけを使う（返答の音声は捨てる）。
    """
    global _transcribe_model_ok
    client = llm.client(config.LIVE_LOCATION).aio.live
    stack = contextlib.AsyncExitStack()
    live = None
    if _transcribe_model_ok is not False:
        try:
            async with asyncio.timeout(TRANSCRIBE_CONNECT_TIMEOUT):
                live = await stack.enter_async_context(
                    client.connect(
                        model=TRANSCRIBE_MODEL,
                        config=types.LiveConnectConfig(
                            response_modalities=[types.Modality.TEXT],
                            input_audio_transcription=types.AudioTranscriptionConfig(language_codes=["ja-JP"]),
                        ),
                    )
                )
            model = TRANSCRIBE_MODEL
            _transcribe_model_ok = True
        except Exception as e:  # pylint: disable=broad-except
            await stack.aclose()
            stack = contextlib.AsyncExitStack()
            _transcribe_model_ok = False  # 使えないと分かったら、以後は試さない
            logger.warning("transcribe model %s unavailable (%s); falling back to %s", TRANSCRIBE_MODEL, e, config.LIVE_MODEL)
    if live is None:
        live = await stack.enter_async_context(
            client.connect(
                model=config.LIVE_MODEL,
                config=types.LiveConnectConfig(
                    response_modalities=[types.Modality.AUDIO],
                    system_instruction=TRANSCRIBE_ONLY_INSTRUCTION,
                    input_audio_transcription=types.AudioTranscriptionConfig(),
                    proactivity=types.ProactivityConfig(proactive_audio=True),
                ),
            )
        )
        model = config.LIVE_MODEL
    try:
        yield live, model
    finally:
        await stack.aclose()


@router.websocket("/ws/transcribe/{session_id}")
async def ws_transcribe(ws: WebSocket, session_id: str) -> None:
    """会議モードの音声入力。Gemini Live API で発話をテキストにし、ブラウザに返す。"""
    await ws.accept()
    if not config.VOICE_INPUT_ENABLED:
        await ws.send_text(json.dumps({"type": "error", "message": "音声入力は無効です"}, ensure_ascii=False))
        await ws.close()
        return
    closed = False
    try:
        async with open_transcriber() as (live, model):
            await ws.send_text(json.dumps({"type": "ready", "model": model}))

            async def upstream() -> None:
                nonlocal closed
                while True:
                    msg = await ws.receive()
                    if msg.get("type") == "websocket.disconnect":
                        closed = True
                        return
                    if msg.get("bytes"):
                        await live.send_realtime_input(
                            audio=types.Blob(data=msg["bytes"], mime_type="audio/pcm;rate=16000")
                        )
                    elif msg.get("text"):
                        data = json.loads(msg["text"])
                        if data.get("type") == "audio_end":
                            await live.send_realtime_input(audio_stream_end=True)
                        elif data.get("type") == "stop":
                            closed = True
                            return

            buf = ""
            last_update = 0.0

            async def flush() -> None:
                nonlocal buf
                if buf.strip():
                    await ws.send_text(json.dumps({"type": "final", "text": buf.strip()}, ensure_ascii=False))
                buf = ""

            async def downstream() -> None:
                nonlocal buf, last_update
                while not closed:
                    async for msg in live.receive():
                        sc = msg.server_content
                        if not sc:
                            continue
                        tr = sc.input_transcription
                        if tr and tr.text:
                            buf += tr.text
                            last_update = time.monotonic()
                            await ws.send_text(json.dumps({"type": "partial", "text": buf}, ensure_ascii=False))
                        # 返答（音声）は使わない。発話の区切り（finished / ターンの区切り）で確定する
                        if (tr and tr.finished) or sc.turn_complete or sc.generation_complete:
                            await flush()

            async def silence_watch() -> None:
                # 区切りの合図が来ないモデルもあるので、文字起こしが一定時間止まったら確定する
                while not closed:
                    await asyncio.sleep(0.3)
                    if buf and time.monotonic() - last_update > SILENCE_FINALIZE_SECONDS:
                        await flush()

            tasks = [asyncio.create_task(c) for c in (upstream(), downstream(), silence_watch())]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except WebSocketDisconnect:
        pass
    except Exception as e:  # pylint: disable=broad-except
        logger.exception("transcribe failed")
        try:
            await ws.send_text(json.dumps({"type": "error", "message": f"文字起こしでエラー: {e}"}, ensure_ascii=False))
        except Exception:  # pylint: disable=broad-except
            pass
    finally:
        try:
            await ws.close()
        except Exception:  # pylint: disable=broad-except
            pass
