"""G2 语音输入输出 —— /api/voice/*

后端 multimodal 已具备 ASR/TTS，此前未暴露给前端。本模块补齐：
  * GET  /api/voice/status       -> ASR/TTS 能力状态
  * POST /api/voice/transcribe   -> 上传音频 → 转写文本（前端语音输入）
  * GET  /api/voice/speak        -> 文字转语音（TTS 返回音频流）
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice", tags=["voice"])

_ALLOWED_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".webm", ".opus"}
_MAX_UPLOAD_BYTES = int(os.getenv("DEADMAN_VOICE_MAX_UPLOAD_MB", "20")) * 1024 * 1024


def _asr_status() -> dict[str, Any]:
    try:
        from ...multimodal.asr import get_asr_service

        svc = get_asr_service()
        enabled = svc.is_enabled()
        return {
            "enabled": enabled,
            "providers": svc.list_providers() if enabled else [],
            "max_upload_bytes": _MAX_UPLOAD_BYTES,
            "allowed_extensions": sorted(_ALLOWED_EXT),
            "supported_languages": ["auto", "zh", "en", "ja", "ko"],
        }
    except Exception as exc:
        return {"enabled": False, "providers": [], "error": str(exc)}


@router.get("/status")
async def voice_status() -> dict[str, Any]:
    return _asr_status()


@router.get("/speak")
async def voice_speak(
    text: str = Query(..., description="要合成的文本"),
    voice_id: str = Query(default="gentle_female", description="音色 id"),
    rate: float = Query(default=1.0, ge=0.5, le=2.0, description="语速 0.5-2.0"),
) -> Response:
    from ...multimodal.tts import VoiceProfile, get_tts_service

    try:
        svc = get_tts_service()
        if not svc.is_enabled():
            return Response(
                content=json.dumps(
                    {"ok": False, "error": "TTS 未启用 (DEADMAN_MULTIMODAL_ENABLED=0)"},
                    ensure_ascii=False,
                ),
                media_type="application/json",
                status_code=503,
            )
        try:
            voice = VoiceProfile(voice_id)
        except ValueError:
            voice = VoiceProfile.GENTLE_FEMALE
        result = await asyncio.to_thread(svc.synthesize, text, voice, rate)
        if result and result.audio:
            return Response(
                content=result.audio,
                media_type="audio/mpeg"
                if getattr(result, "format", None) == "mp3"
                else "audio/wav",
            )
        return Response(
            content=json.dumps(
                {"ok": False, "error": "TTS 合成失败（无音频输出）"}, ensure_ascii=False
            ),
            media_type="application/json",
            status_code=500,
        )
    except Exception as exc:
        return Response(
            content=json.dumps({"ok": False, "error": f"TTS 失败: {exc}"}, ensure_ascii=False),
            media_type="application/json",
            status_code=500,
        )


@router.post("/transcribe")
async def voice_transcribe(
    audio: UploadFile = File(default=None, description="音频文件"),  # noqa: B008
    language: str = Form(default="auto", description="语言：auto/zh/en/ja/ko"),
) -> dict[str, Any]:
    if audio is None:
        raise HTTPException(status_code=400, detail="缺少音频文件")
    original = audio.filename or "recording.webm"
    ext = Path(original).suffix.lower()
    if ext not in _ALLOWED_EXT:
        raise HTTPException(
            status_code=415,
            detail=f"不支持的音频格式: {ext or '(无扩展名)'}，允许: {sorted(_ALLOWED_EXT)}",
        )
    data = await audio.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"音频过大（{len(data) / 1024 / 1024:.1f}MB），上限 {_MAX_UPLOAD_BYTES // 1024 // 1024}MB",
        )
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="音频为空")

    tmp_path = Path(tempfile.mktemp(prefix="deadman_voice_", suffix=ext))
    tmp_path.write_bytes(data)
    try:
        from ...multimodal.asr import get_asr_service

        svc = get_asr_service()
        if not svc.is_enabled():
            raise HTTPException(
                status_code=503, detail="语音转写未启用（DEADMAN_MULTIMODAL_ENABLED=0）"
            )
        try:
            result = await asyncio.to_thread(svc.transcribe, tmp_path, language or "auto")
        except Exception as exc:
            logger.warning("ASR 转写异常: %s", exc)
            raise HTTPException(status_code=500, detail=f"转写失败: {exc}") from exc
        return {
            "ok": True,
            "text": result.text,
            "language": result.language,
            "confidence": round(result.confidence, 4),
            "provider": result.provider,
        }
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
