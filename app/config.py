"""アプリ全体の設定。すべて環境変数で上書きできる。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = Path(os.getenv("KNOWLEDGE_DIR", ROOT / "knowledge"))
FRONTEND_DIR = Path(os.getenv("FRONTEND_DIR", ROOT / "frontend"))


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# --- モデル -----------------------------------------------------------------
# テキストの会話・進行役・準備は、レイテンシに有利な最新の Flash を使う。
TEXT_MODEL = os.getenv("TEXT_MODEL", "gemini-3.8-flash")
# 進行役（誰が話すかの判断）に使うモデル。1秒前後で返る最新の Flash-Lite を使う。
DIRECTOR_MODEL = os.getenv("DIRECTOR_MODEL", "gemini-3.5-flash-lite")
# 準備（資料選び・ペルソナの事前の考え）に使うモデル。
PREP_MODEL = os.getenv("PREP_MODEL", TEXT_MODEL)
# 思考レベル（MINIMAL / LOW / MEDIUM / HIGH）。低いほど速い。
# gemini-3.8-flash は MINIMAL に対応していない（2026-11 時点で確認）。
PERSONA_THINKING_LEVEL = os.getenv("PERSONA_THINKING_LEVEL", "LOW")
DIRECTOR_THINKING_LEVEL = os.getenv("DIRECTOR_THINKING_LEVEL", "MINIMAL")
# 音声会話（Gemini Live API）のモデルとリージョン。
LIVE_MODEL = os.getenv("LIVE_MODEL", "gemini-3.8-live")
LIVE_LOCATION = os.getenv("LIVE_LOCATION", "us-central1")
# 会議モードの音声入力（文字起こし専用の Live API モデル）。
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gemini-3.5-transcribe-live")

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "global")

# --- 機能の切り替え ---------------------------------------------------------
# 音声入力（Gemini Live）。既定は on。
VOICE_INPUT_ENABLED = _bool("VOICE_INPUT_ENABLED", True)
# 設定できる会話時間の上限（分）。
MAX_DURATION_MINUTES = int(os.getenv("MAX_DURATION_MINUTES", "5"))
# 残り何秒でまとめを促すか／何秒前に結論を出すか。
WRAPUP_SECONDS = int(os.getenv("WRAPUP_SECONDS", "60"))
FINAL_CALL_SECONDS = int(os.getenv("FINAL_CALL_SECONDS", "10"))
# 会議モードで、ユーザーの1発言に続けて AI が話す最大ターン数（AI 同士の掛け合いを含む）。
MAX_AI_TURNS_PER_ROUND = int(os.getenv("MAX_AI_TURNS_PER_ROUND", "4"))
# 会話のシステムプロンプトに入れる資料の上限（文字）。
MAX_DOC_CHARS = int(os.getenv("MAX_DOC_CHARS", "90000"))
# 添付ファイルの上限（MB）。
MAX_ATTACHMENT_MB = int(os.getenv("MAX_ATTACHMENT_MB", "15"))
# IAP がない環境（ローカル）での利用者 ID。
LOCAL_USER_ID = os.getenv("LOCAL_USER_ID", "local-user")
