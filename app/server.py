"""Web UI 用の API（REST・WebSocket）と静的ファイル。

- GET  /                     Web UI
- GET  /api/config           UI の設定（キャラクター、音声入力の on/off、時間の上限）
- POST /api/sessions         準備（ローディング）を進捗つきでストリーミングし、セッションを作る
- GET  /api/sessions/{id}    セッションの状態と会話履歴
- POST /api/sessions/{id}/attachments  添付ファイルを提出する
- WS   /ws/sessions/{id}     テキストの会話（進行・タイマー・割り込みもここ）
- WS   /ws/live/{id}         対話モードの音声会話（Gemini Live API）
- WS   /ws/transcribe/{id}   会議モードの音声入力（Gemini Live API で文字起こし）

認証は Cloud Run の IAP に任せる。IAP が付けるヘッダーから利用者を識別する。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
)
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.genai import types
from starlette.websockets import WebSocketDisconnect

from app import config, conversation, llm, runtime
from app.knowledge import get_bundle
from app.preparation import PrepRequest, prepare
from app.summary import has_conversation, is_closed, make_summary

logger = logging.getLogger(__name__)

APP_NAME = "app"
IAP_EMAIL_HEADER = "x-goog-authenticated-user-email"
PUBLIC_STATE_KEYS = (
    "mode",
    "persona_ids",
    "chair",
    "theme",
    "theme_summary",
    "decision_question",
    "issues",
    "doc_ids",
    "duration_sec",
    "deadline",
    "phase",
    "decision",
    "verdict",
    "attachments",
    "user_name",
    "user_role",
    "prep_seconds",
    "summary",
)
ALLOWED_MIME_PREFIXES = ("image/", "text/", "application/pdf", "audio/", "video/")


def user_id_from(headers: Any) -> str:
    raw = headers.get(IAP_EMAIL_HEADER) or ""
    if raw:
        return raw.split(":", 1)[-1]
    return config.LOCAL_USER_ID


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    out = {k: state.get(k) for k in PUBLIC_STATE_KEYS}
    bundle = get_bundle()
    out["documents"] = [
        {"id": d, "title": bundle.concepts[d].title, "type": bundle.concepts[d].type}
        for d in state.get("doc_ids", [])
        if d in bundle.concepts
    ]
    out["issue_titles"] = [bundle.issue_title(i) for i in state.get("issues", [])]
    out["server_time"] = time.time()
    return out


def event_text(ev: Event) -> str:
    if not ev.content or not ev.content.parts:
        return ""
    return "".join(p.text or "" for p in ev.content.parts if p.text and not p.thought).strip()


def history(events: list[Event]) -> list[dict[str, Any]]:
    personas = get_bundle().personas
    out = []
    for ev in events:
        if ev.partial:
            continue
        text = event_text(ev)
        if not text:
            continue
        if ev.author == "user":
            if text.startswith(conversation.CUE_PREFIX):
                continue
            out.append({"id": ev.id, "speaker": "user", "text": text, "ts": ev.timestamp})
        elif ev.author in personas:
            out.append({"id": ev.id, "speaker": ev.author, "text": text, "ts": ev.timestamp})
    return out


class Services:
    """fast_api_app の lifespan で作った Runner を受け取って使う。"""

    runner: Runner | None = None

    @property
    def session_service(self):
        assert self.runner is not None
        return self.runner.session_service

    @property
    def artifact_service(self):
        assert self.runner is not None
        return self.runner.artifact_service


services = Services()
router = APIRouter()


async def load_session(user_id: str, session_id: str):
    session = await services.session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


async def append_state(session, delta: dict[str, Any], author: str = "system") -> None:
    await services.session_service.append_event(
        session,
        Event(invocation_id=f"e-{uuid.uuid4()}", author=author, actions=EventActions(state_delta=delta)),
    )


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------
@router.get("/api/config")
async def get_config(request: Request) -> dict[str, Any]:
    bundle = get_bundle()
    return {
        "personas": [p.to_public() for p in bundle.personas.values()],
        "voice_input_enabled": config.VOICE_INPUT_ENABLED,
        "max_duration_minutes": config.MAX_DURATION_MINUTES,
        "wrapup_seconds": config.WRAPUP_SECONDS,
        "final_call_seconds": config.FINAL_CALL_SECONDS,
        "max_attachment_mb": config.MAX_ATTACHMENT_MB,
        "models": {"text": config.TEXT_MODEL, "director": config.DIRECTOR_MODEL, "live": config.LIVE_MODEL},
        "user": user_id_from(request.headers),
        "issues": bundle.open_issues,
    }


@router.post("/api/sessions")
async def create_session(req: PrepRequest, request: Request) -> StreamingResponse:
    user_id = user_id_from(request.headers)
    bundle = get_bundle()
    if not req.persona_ids or any(p not in bundle.personas for p in req.persona_ids):
        raise HTTPException(status_code=400, detail="persona_ids is invalid")
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def progress(step: str, label: str, extra: dict[str, Any] | None) -> None:
        await queue.put({"type": "progress", "step": step, "label": label, **(extra or {})})

    async def work() -> None:
        try:
            state = await prepare(req, progress)
            session = await services.session_service.create_session(
                app_name=APP_NAME, user_id=user_id, state=state
            )
            await queue.put({"type": "session", "session_id": session.id, "state": public_state(state)})
        except Exception as e:  # pylint: disable=broad-except
            logger.exception("prepare failed")
            await queue.put({"type": "error", "message": f"準備に失敗しました: {e}"})
        finally:
            await queue.put(None)

    async def stream():
        task = asyncio.create_task(work())
        try:
            while (item := await queue.get()) is not None:
                yield json.dumps(item, ensure_ascii=False) + "\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# ---------------------------------------------------------------------------
# 相談の一覧・サマリ・キャンセル
# ---------------------------------------------------------------------------
MAX_SUMMARIES_PER_LIST = 6
IDLE_SUMMARY_SECONDS = 30 * 60


def summary_is_stale(state: dict[str, Any], last_update_time: float) -> bool:
    summary = state.get("summary")
    # サマリの保存自体でも更新時刻が進むので、少し余裕を見る
    return not summary or float(summary.get("created_at", 0)) < last_update_time - 60
_summary_tasks: set[asyncio.Task] = set()


async def ensure_summary(user_id: str, session_id: str) -> dict[str, Any] | None:
    """サマリがなければ作ってセッションに保存する。会話がなければ作らない。"""
    session = await load_session(user_id, session_id)
    state = session.state
    if not has_conversation(state) or not summary_is_stale(state, session.last_update_time):
        return state.get("summary")
    summary = await make_summary(dict(state), list(session.events))
    await append_state(session, {"summary": summary})
    return summary


def schedule_summary(user_id: str, session_id: str) -> None:
    async def run() -> None:
        try:
            await ensure_summary(user_id, session_id)
        except Exception:  # pylint: disable=broad-except
            logger.exception("summary failed for %s", session_id)

    task = asyncio.create_task(run())
    _summary_tasks.add(task)
    task.add_done_callback(_summary_tasks.discard)


def history_item(session) -> dict[str, Any]:
    state = session.state or {}
    bundle = get_bundle()
    verdict = state.get("verdict") or {}
    phase = state.get("phase")
    if phase == "cancelled":
        status = "キャンセル"
    elif verdict:
        status = verdict.get("label", "")
    elif phase in (None, "waiting"):
        status = "未開始"
    else:
        status = "進行中"
    return {
        "session_id": session.id,
        "updated_at": session.last_update_time,
        "mode": state.get("mode"),
        "persona_ids": [p for p in state.get("persona_ids", []) if p in bundle.personas],
        "theme": state.get("theme") or state.get("theme_summary") or "",
        "phase": phase,
        "status": status,
        "verdict": verdict or None,
        "summary": state.get("summary"),
    }


@router.get("/api/sessions")
async def list_sessions(request: Request) -> dict[str, Any]:
    user_id = user_id_from(request.headers)
    resp = await services.session_service.list_sessions(app_name=APP_NAME, user_id=user_id)
    # 会話が始まっていないもの（準備だけで終わったもの）は出さない
    sessions = [s for s in resp.sessions if (s.state or {}).get("persona_ids") and has_conversation(s.state)]
    sessions.sort(key=lambda s: s.last_update_time, reverse=True)
    # サマリがない（または古い）、会話のあったセッションは、ここでまとめて作る（上限あり）。
    # 進行中のものは、30分以上動きがなければ対象にする
    now = time.time()
    missing = [
        s for s in sessions
        if has_conversation(s.state)
        and (is_closed(s.state) or now - s.last_update_time > IDLE_SUMMARY_SECONDS)
        and summary_is_stale(s.state, s.last_update_time)
    ][:MAX_SUMMARIES_PER_LIST]
    results = await asyncio.gather(*(ensure_summary(user_id, s.id) for s in missing), return_exceptions=True)
    for s, res in zip(missing, results, strict=True):
        if isinstance(res, dict):
            s.state["summary"] = res
        elif isinstance(res, BaseException):
            logger.error("summary failed for %s: %s", s.id, res)
    return {"user": user_id, "items": [history_item(s) for s in sessions]}


@router.post("/api/sessions/{session_id}/cancel")
async def cancel_session(session_id: str, request: Request) -> dict[str, Any]:
    user_id = user_id_from(request.headers)
    session = await load_session(user_id, session_id)
    runtime.interrupt_event(session_id).set()
    if not is_closed(session.state):
        await append_state(session, {"phase": "cancelled", "cancelled_at": time.time()})
    schedule_summary(user_id, session_id)
    return {"session_id": session_id, "phase": "cancelled" if not session.state.get("verdict") else session.state.get("phase")}


@router.get("/api/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> dict[str, Any]:
    session = await load_session(user_id_from(request.headers), session_id)
    return {"session_id": session.id, "state": public_state(session.state), "messages": history(session.events)}


@router.post("/api/sessions/{session_id}/attachments")
async def upload_attachment(
    session_id: str,
    request: Request,
    file: Annotated[UploadFile, File()],
    note: Annotated[str, Form()] = "",
) -> JSONResponse:
    user_id = user_id_from(request.headers)
    session = await load_session(user_id, session_id)
    data = await file.read()
    if len(data) > config.MAX_ATTACHMENT_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"{config.MAX_ATTACHMENT_MB}MB までです")
    mime = file.content_type or "application/octet-stream"
    if mime in ("application/octet-stream", "") and (file.filename or "").endswith((".md", ".txt", ".csv")):
        mime = "text/plain"
    if not mime.startswith(ALLOWED_MIME_PREFIXES):
        raise HTTPException(status_code=415, detail=f"この形式には対応していません: {mime}")
    name = (file.filename or "attachment").replace("/", "_")[:120]
    part = types.Part(inline_data=types.Blob(data=data, mime_type=mime, display_name=name))
    await services.artifact_service.save_artifact(
        app_name=APP_NAME, user_id=user_id, session_id=session_id, filename=name, artifact=part
    )
    try:
        digest = await llm.generate_text(
            model=config.DIRECTOR_MODEL,
            prompt=[
                types.Part(text="次の資料の要点を、日本語で200字以内にまとめてください。数字は資料にあるものだけ。"),
                types.Part(inline_data=types.Blob(data=data, mime_type=mime)),
            ],
            thinking_level=config.DIRECTOR_THINKING_LEVEL,
        )
    except Exception:  # pylint: disable=broad-except
        logger.exception("digest failed")
        digest = "（要約できませんでした）"
    attachments = [a for a in (session.state.get("attachments") or []) if a.get("name") != name]
    attachments.append({"name": name, "mime": mime, "size": len(data), "digest": digest, "note": note})
    await append_state(session, {"attachments": attachments})
    return JSONResponse({"name": name, "mime": mime, "size": len(data), "digest": digest})


# ---------------------------------------------------------------------------
# テキスト会話（WebSocket）
# ---------------------------------------------------------------------------
class ChatController:
    """1 本の WebSocket 接続につき 1 つ。発言と進行の合図を順番に処理する。"""

    MAX_IDLE_IN_A_ROW = 2

    def __init__(self, ws: WebSocket, user_id: str, session_id: str):
        self.ws = ws
        self.user_id = user_id
        self.session_id = session_id
        self.queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.busy = False
        self.idle_count = 0
        self.state: dict[str, Any] = {}
        self.closed = False
        self.timer_task: asyncio.Task | None = None

    async def send(self, payload: dict[str, Any]) -> None:
        if self.closed:
            return
        try:
            await self.ws.send_text(json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:  # pylint: disable=broad-except
            self.closed = True

    async def run(self) -> None:
        session = await load_session(self.user_id, self.session_id)
        self.state = dict(session.state)
        await self.send({"type": "state", "state": public_state(self.state)})
        self.ensure_timer()
        worker = asyncio.create_task(self.worker())
        try:
            while True:
                msg = json.loads(await self.ws.receive_text())
                await self.on_client_message(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            self.closed = True
            worker.cancel()
            if self.timer_task:
                self.timer_task.cancel()

    async def on_client_message(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if is_closed(self.state):
            await self.send({"type": "ended", "state": public_state(self.state)})
            return
        if kind == "user_message":
            text = str(msg.get("text", "")).strip()
            if not text or text.startswith(conversation.CUE_PREFIX):
                return
            self.idle_count = 0
            if self.busy:  # 割り込み: 進行中のラウンドの残りを打ち切る
                runtime.interrupt_event(self.session_id).set()
            await self.queue.put(("user", text))
        elif kind == "idle":
            if self.busy or not self.queue.empty() or self.idle_count >= self.MAX_IDLE_IN_A_ROW:
                return
            if self.state.get("phase") in (None, "waiting", "closing", "ended", "cancelled"):
                return
            self.idle_count += 1
            await self.queue.put(("cue", conversation.CUE_IDLE))
        elif kind == "finish":
            # 時間前に結論が出たので締める（結論が出ていないときは何もしない）
            if (self.state.get("decision") or {}).get("status", "undecided") == "undecided":
                await self.send({"type": "error", "message": "まだ結論が出ていません"})
                return
            runtime.interrupt_event(self.session_id).set()
            await self.queue.put(("cue", conversation.CUE_FINISH))
        elif kind == "ping":
            await self.send({"type": "pong", "server_time": time.time()})

    def ensure_timer(self) -> None:
        if self.timer_task or not self.state.get("deadline") or self.state.get("final_done"):
            return
        self.timer_task = asyncio.create_task(self.timer())

    async def timer(self) -> None:
        deadline = float(self.state["deadline"])
        duration = int(self.state.get("duration_sec") or 0)
        if duration > config.WRAPUP_SECONDS + 30 and not self.state.get("wrapup_done"):
            await asyncio.sleep(max(0.0, deadline - config.WRAPUP_SECONDS - time.time()))
            if not self.state.get("wrapup_done") and not is_closed(self.state):
                runtime.interrupt_event(self.session_id).set()  # 進行中の掛け合いを切り上げて、まとめを優先する
                await self.queue.put(("cue", conversation.CUE_WRAPUP))
        await asyncio.sleep(max(0.0, deadline - config.FINAL_CALL_SECONDS - time.time()))
        if not self.state.get("final_done") and not is_closed(self.state):
            runtime.interrupt_event(self.session_id).set()
            await self.queue.put(("cue", conversation.CUE_FINAL))

    async def worker(self) -> None:
        while not self.closed:
            kind, payload = await self.queue.get()
            # 音声会話など、別の接続で state が進んでいることがあるので取り直す
            try:
                session = await load_session(self.user_id, self.session_id)
                self.state = dict(session.state)
                self.ensure_timer()
            except Exception:  # pylint: disable=broad-except
                logger.exception("failed to refresh session state")
            if is_closed(self.state):
                await self.send({"type": "ended", "state": public_state(self.state)})
                continue
            text = payload if kind == "user" else conversation.CUE_TEXT[payload]
            self.busy = True
            try:
                await self.run_round(text)
            except Exception as e:  # pylint: disable=broad-except
                logger.exception("round failed")
                await self.send({"type": "error", "message": f"応答の生成に失敗しました: {e}"})
            finally:
                self.busy = False
                await self.send({"type": "idle_ok"})

    async def run_round(self, text: str) -> None:
        assert services.runner is not None
        current: dict[str, str] = {}
        async for ev in services.runner.run_async(
            user_id=self.user_id,
            session_id=self.session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=text)]),
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        ):
            delta = ev.actions.state_delta if ev.actions else None
            if delta:
                self.state.update(delta)
                await self.send({"type": "state", "state": public_state(self.state)})
                if "last_plan" in delta:
                    await self.send({"type": "plan", "turns": delta["last_plan"]})
                if "deadline" in delta:
                    self.ensure_timer()
            if ev.author not in get_bundle().personas:
                continue
            txt = event_text(ev)
            if not txt:
                continue
            msg_id = current.setdefault(ev.author, f"m-{uuid.uuid4().hex[:10]}")
            if ev.partial:
                await self.send({"type": "delta", "id": msg_id, "speaker": ev.author, "text": txt})
            else:
                await self.send({"type": "message", "id": msg_id, "speaker": ev.author, "text": txt})
                current.pop(ev.author, None)
        if self.state.get("phase") == "ended":
            await self.send({"type": "ended", "state": public_state(self.state)})
            schedule_summary(self.user_id, self.session_id)


@router.websocket("/ws/sessions/{session_id}")
async def ws_chat(ws: WebSocket, session_id: str) -> None:
    await ws.accept()
    controller = ChatController(ws, user_id_from(ws.headers), session_id)
    try:
        await controller.run()
    except HTTPException:
        await ws.send_text(json.dumps({"type": "error", "message": "セッションが見つかりません"}, ensure_ascii=False))
        await ws.close()


def mount_playground(app: FastAPI) -> None:
    """ADK の開発用 UI（agents-cli playground）を /playground から開けるようにする。

    ADK は / を /dev-ui/ へリダイレクトするルートを登録するので、それを外して
    / は本番の Web UI に譲る。開発用 UI の本体は /dev-ui/ のまま配信される。
    """
    app.router.routes[:] = [
        r for r in app.router.routes
        if not (getattr(r, "path", None) == "/" and getattr(r, "name", "") == "redirect_root_to_dev_ui")
    ]
    has_dev_ui = any(getattr(r, "path", "") == "/dev-ui" for r in app.router.routes)

    @app.get("/playground", include_in_schema=False)
    @app.get("/playground/", include_in_schema=False)
    async def playground() -> Response:
        if not has_dev_ui:
            return JSONResponse({"detail": "playground is disabled (ADK_DEV_UI=false)"}, status_code=404)
        return RedirectResponse(f"/dev-ui/?app={APP_NAME}")


def mount_frontend(app: FastAPI) -> None:
    app.mount("/static", StaticFiles(directory=config.FRONTEND_DIR), name="frontend")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(config.FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}


def install(app: FastAPI, runner: Runner) -> None:
    services.runner = runner
