"""多模态工具适配 - 把 MultimodalPipeline 五能力暴露为 agent 工具

能力实现全部在 ``deadman.multimodal``（provider 懒加载 + 降级），
本模块只做工具化包装：
    - ocr_extract      图片文字提取（输出已 PII 脱敏）
    - asr_transcribe   语音转文字
    - text_to_speech   文字转语音（返回 base64 音频）
    - analyze_image    视觉理解/图像描述
    - generate_image   文生图

统一 envelope：ok / error；依赖缺失或 flag 关闭时 ok=False + 提示，不抛异常
（integrity-framework）。各 provider 依赖见 pyproject ``multimodal`` extra。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from ..multimodal.pipeline import MultimodalDisabledError, get_multimodal_pipeline


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _file_guard(path: str) -> dict[str, Any] | None:
    """文件存在性守卫；返回错误 envelope 或 None（通过）"""
    if not path or not Path(path).is_file():
        return {"ok": False, "error": f"文件不存在: {path}"}
    return None


def tool_ocr_extract(image_path: str, user_id: str = "unknown") -> dict[str, Any]:
    """OCR：从图片提取文字（自动 PII 脱敏）"""
    guard = _file_guard(image_path)
    if guard:
        return guard
    try:
        result = get_multimodal_pipeline().ocr_extract(image_path=Path(image_path), user_id=user_id)
    except MultimodalDisabledError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, **result.to_dict()}


def tool_asr_transcribe(
    audio_path: str, language: str = "auto", user_id: str = "unknown"
) -> dict[str, Any]:
    """ASR：语音文件转文字"""
    guard = _file_guard(audio_path)
    if guard:
        return guard
    try:
        result = get_multimodal_pipeline().asr_transcribe(
            audio_path=Path(audio_path), language=language, user_id=user_id
        )
    except MultimodalDisabledError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, **result.to_dict()}


def tool_text_to_speech(text: str, user_id: str = "unknown") -> dict[str, Any]:
    """TTS：文字转语音，返回 base64 音频"""
    if not text or not text.strip():
        return {"ok": False, "error": "text 不能为空"}
    try:
        result = get_multimodal_pipeline().tts_synthesize(text=text, user_id=user_id)
    except MultimodalDisabledError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    payload = result.to_dict()
    audio_b64 = _b64(result.audio_bytes) if result.audio_bytes else ""
    return {
        "ok": True,
        **{k: v for k, v in payload.items() if k != "audio_bytes"},
        "bytes": len(result.audio_bytes) if result.audio_bytes else 0,
        "audio_base64": audio_b64,
    }


def tool_analyze_image(
    image_path: str, question: str = "", user_id: str = "unknown"
) -> dict[str, Any]:
    """视觉理解：描述图片内容 / 回答关于图片的问题"""
    guard = _file_guard(image_path)
    if guard:
        return guard
    try:
        description: str = get_multimodal_pipeline().vision_describe(
            image_path=Path(image_path),
            prompt=question if question.strip() else "请描述这张图片的内容",
            user_id=user_id,
        )
    except MultimodalDisabledError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "description": description}


def tool_generate_image(prompt: str, user_id: str = "unknown") -> dict[str, Any]:
    """文生图：按提示词生成图片，返回 base64 PNG"""
    if not prompt or not prompt.strip():
        return {"ok": False, "error": "prompt 不能为空"}
    try:
        image_bytes: bytes = get_multimodal_pipeline().image_gen_generate(
            prompt=prompt, user_id=user_id
        )
    except MultimodalDisabledError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "format": "png",
        "bytes": len(image_bytes),
        "image_base64": _b64(image_bytes),
    }
