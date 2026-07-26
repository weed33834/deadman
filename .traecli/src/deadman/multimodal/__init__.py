"""multimodal - 多模态支持(OCR / ASR / TTS / Vision / ImageGen / Storage)。

P8.2 多模态框架,为 deadman 项目增加图片 / 音频处理能力。

模块结构:
    - ocr.py:        OCR 服务(图片识别 - 证件 / 文档)
    - asr.py:        ASR 服务(语音转文字)
    - tts.py:        TTS 服务(文字转语音 - 悼文朗读)
    - vision.py:     Vision 服务(图片理解 - GPT-4V / Claude / Qwen-VL)
    - image_gen.py:  Image Generator(图片生成 - 讣告 / 纪念卡片)
    - storage.py:    多模态文件存储(TTL 清理)
    - pipeline.py:   编排层(audit + PII + budget)

设计原则:
    - 所有 provider 可选,懒加载,缺失时降级到 mock / manual
    - feature flag DEADMAN_MULTIMODAL_ENABLED=0(默认 OFF)
    - 线程安全(threading.RLock 保护状态)
    - 持久化用原子写(tmp + os.replace)
    - deadman 场景的嗓音 / 风格预设经特别设计(温和 / 庄重,避免冒犯丧属)

合规关联:
    - PIPL:OCR 提取的文本默认走 PIIRedactor 脱敏(pipeline 集成)
    - service-boundary-framework.md:返回结果附"AI 生成仅供参考"边界告知
    - safety-protocol.md:图片描述 / 生成不评判逝者外貌

feature flag:`DEADMAN_MULTIMODAL_ENABLED=0`(默认 OFF)
"""

from __future__ import annotations

from .asr import ASRResult, ASRService, ASRSegment
from .image_gen import ImageGenerator, ImageSize, ImageStyle, STYLE_PRESETS
from .ocr import DocType, OCRResult, OCRService
from .pipeline import (
    AuditEntry,
    MultimodalConfig,
    MultimodalDisabledError,
    MultimodalPipeline,
    get_multimodal_pipeline,
    reset_multimodal_pipeline,
)
from .storage import FileMetadata, MultimodalStorage, get_multimodal_storage
from .tts import AudioFormat, TTSResult, TTSService, VoiceProfile
from .vision import DetectedObject, VisionDescription, VisionService

__all__ = [
    # OCR
    "OCRService",
    "OCRResult",
    "DocType",
    # ASR
    "ASRService",
    "ASRResult",
    "ASRSegment",
    # TTS
    "TTSService",
    "TTSResult",
    "VoiceProfile",
    "AudioFormat",
    # Vision
    "VisionService",
    "VisionDescription",
    "DetectedObject",
    # Image gen
    "ImageGenerator",
    "ImageStyle",
    "ImageSize",
    "STYLE_PRESETS",
    # Storage
    "MultimodalStorage",
    "FileMetadata",
    "get_multimodal_storage",
    # Pipeline
    "MultimodalPipeline",
    "MultimodalConfig",
    "MultimodalDisabledError",
    "AuditEntry",
    "get_multimodal_pipeline",
    "reset_multimodal_pipeline",
]
