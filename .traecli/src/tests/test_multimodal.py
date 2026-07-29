"""P8.2 多模态框架测试 - OCR / ASR / TTS / Vision / ImageGen / Storage / Pipeline。

覆盖:
    - 各 service 实例化(mock provider)
    - OCR 提取 + PII 检测集成
    - ASR 转写
    - TTS 合成
    - Vision 描述 / 物体抽取
    - Image gen
    - Pipeline 路由 / audit / budget
    - Multimodal storage(store/retrieve/delete/cleanup)
    - Disabled state raises MultimodalDisabledError

feature flag:`DEADMAN_MULTIMODAL_ENABLED=1`(测试启用)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# =====================================================================
# fixtures
# =====================================================================


@pytest.fixture(autouse=True)
def enable_multimodal(monkeypatch, tmp_path):
    """启用 multimodal feature flag,并隔离文件存储到 tmp_path。"""
    monkeypatch.setenv("DEADMAN_MULTIMODAL_ENABLED", "1")
    monkeypatch.setenv("DEADMAN_DEFENSE_ENABLED", "1")
    monkeypatch.setenv("DEADMAN_CIRCUIT_BREAKER_ENABLED", "1")
    monkeypatch.setenv("DEADMAN_FEATURE_FLAG_SYSTEM_ENABLED", "1")
    # 隔离 HOME,避免污染真实 ~/.deadman
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)

    from deadman.infrastructure.feature_flags import get_flags
    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0

    # 重置 defense 全局单例(budget_coordinator 等)
    import deadman.infrastructure.defense as defense_pkg
    defense_pkg._bc_instance = None
    defense_pkg._pr_instance = None

    # 重置 multimodal 全局单例
    from deadman.multimodal import (
        ocr, asr, tts, vision, image_gen, storage, pipeline,
    )
    ocr._ocr_instance = None
    asr._asr_instance = None
    tts._tts_instance = None
    vision._vision_instance = None
    image_gen._imgen_instance = None
    storage._storage_instance = None
    pipeline._pipeline_instance = None

    yield

    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    defense_pkg._bc_instance = None
    defense_pkg._pr_instance = None
    ocr._ocr_instance = None
    asr._asr_instance = None
    tts._tts_instance = None
    vision._vision_instance = None
    image_gen._imgen_instance = None
    storage._storage_instance = None
    pipeline._pipeline_instance = None


@pytest.fixture
def sample_image(tmp_path):
    """创建一个简单的测试图片文件。"""
    p = tmp_path / "sample.png"
    # 8x8 PNG 占位
    p.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x08"
        b"\x00\x00\x00\x08\x08\x06\x00\x00\x00\xc4\x0f\xbe\x8b"
        b"\x00\x00\x00\x1aIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x03"
        b"\x00\x01\x5d\xcc\xdb\xd2\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return p


@pytest.fixture
def sample_audio(tmp_path):
    """创建一个简单的测试音频文件。"""
    p = tmp_path / "sample.mp3"
    p.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00MOCK-AUDIO-CONTENT")
    return p


# =====================================================================
# OCR tests
# =====================================================================


class TestOCRService:
    def test_instantiation_default_providers(self):
        from deadman.multimodal.ocr import OCRService
        svc = OCRService()
        names = svc.list_providers()
        assert "cloud" in names
        assert "tesseract" in names
        assert "manual" in names

    def test_is_enabled_when_flag_on(self):
        from deadman.multimodal.ocr import OCRService
        svc = OCRService()
        assert svc.is_enabled() is True

    def test_extract_id_card_with_mock(self, sample_image):
        from deadman.multimodal.ocr import DocType, OCRService
        svc = OCRService()
        result = svc.extract(sample_image, DocType.ID_CARD)
        assert result.doc_type == DocType.ID_CARD
        assert result.text  # mock 返回非空
        assert result.provider == "cloud"  # mock cloud 总是首个可用
        assert "张三" in result.text

    def test_extract_death_certificate_fields(self, sample_image):
        from deadman.multimodal.ocr import DocType, OCRService
        svc = OCRService()
        result = svc.extract(sample_image, DocType.DEATH_CERTIFICATE)
        assert result.doc_type == DocType.DEATH_CERTIFICATE
        assert "decedent_name" in result.fields
        assert "death_date" in result.fields

    def test_extract_passport(self, sample_image):
        from deadman.multimodal.ocr import DocType, OCRService
        svc = OCRService()
        result = svc.extract(sample_image, DocType.PASSPORT)
        assert "passport_number" in result.fields

    def test_extract_will(self, sample_image):
        from deadman.multimodal.ocr import DocType, OCRService
        svc = OCRService()
        result = svc.extract(sample_image, DocType.WILL)
        assert "testator" in result.fields

    def test_extract_nonexistent_file_returns_empty(self, tmp_path):
        from deadman.multimodal.ocr import DocType, OCRService
        svc = OCRService()
        result = svc.extract(tmp_path / "nope.png", DocType.OTHER)
        assert result.text == ""
        assert result.confidence == 0.0
        assert result.provider == "none"

    def test_register_custom_provider(self, sample_image):
        from deadman.multimodal.ocr import (
            DocType, OCRProvider, OCRResult, OCRService,
        )

        class CustomProvider(OCRProvider):
            name = "custom"
            def is_available(self) -> bool:
                return True
            def extract(self, image_path, doc_type):
                return OCRResult(
                    text="custom", confidence=0.99,
                    doc_type=doc_type, provider=self.name,
                )

        svc = OCRService()
        svc.register_provider(CustomProvider(), position=0)
        assert svc.list_providers()[0] == "custom"
        result = svc.extract(sample_image, DocType.OTHER)
        assert result.provider == "custom"

    def test_extract_fallback_to_manual(self, sample_image):
        """cloud / tesseract 都不可用时,fallback 到 manual。"""
        from deadman.multimodal.ocr import (
            DocType, OCRProvider, OCRResult, OCRService,
        )

        class FailingCloud(OCRProvider):
            name = "cloud_fail"
            def is_available(self) -> bool:
                return True
            def extract(self, image_path, doc_type):
                raise RuntimeError("cloud down")

        class ManualOK(OCRProvider):
            name = "manual_ok"
            def is_available(self) -> bool:
                return True
            def extract(self, image_path, doc_type):
                return OCRResult(
                    text="manual-input", confidence=0.0,
                    doc_type=doc_type, provider=self.name,
                )

        svc = OCRService(custom_providers=[FailingCloud(), ManualOK()])
        result = svc.extract(sample_image, DocType.OTHER)
        assert result.provider == "manual_ok"


# =====================================================================
# ASR tests
# =====================================================================


class TestASRService:
    def test_instantiation(self):
        from deadman.multimodal.asr import ASRService
        svc = ASRService()
        assert "cloud" in svc.list_providers()

    def test_transcribe_chinese(self, sample_audio):
        from deadman.multimodal.asr import ASRService
        svc = ASRService()
        result = svc.transcribe(sample_audio, language="zh")
        assert result.language == "zh"
        assert result.text
        assert result.provider == "cloud"

    def test_transcribe_auto_detect(self, sample_audio):
        from deadman.multimodal.asr import ASRService
        svc = ASRService()
        result = svc.transcribe(sample_audio, language="auto")
        # mock cloud 对 auto 返回 zh
        assert result.language == "zh"

    def test_transcribe_unsupported_lang_fallback_to_auto(self, sample_audio):
        from deadman.multimodal.asr import ASRService
        svc = ASRService()
        result = svc.transcribe(sample_audio, language="fr")
        assert result.language in ("zh", "auto")

    def test_transcribe_nonexistent_file(self, tmp_path):
        from deadman.multimodal.asr import ASRService
        svc = ASRService()
        result = svc.transcribe(tmp_path / "nope.mp3")
        assert result.text == ""
        assert result.confidence == 0.0

    def test_segments_have_timestamps(self, sample_audio):
        from deadman.multimodal.asr import ASRService
        svc = ASRService()
        result = svc.transcribe(sample_audio, language="zh")
        assert len(result.segments) >= 1
        assert result.segments[0].start >= 0
        assert result.segments[0].end > result.segments[0].start


# =====================================================================
# TTS tests
# =====================================================================


class TestTTSService:
    def test_instantiation(self):
        from deadman.multimodal.tts import TTSService
        svc = TTSService()
        assert "mock" in svc.list_providers()

    def test_synthesize_returns_bytes(self):
        from deadman.multimodal.tts import TTSService, VoiceProfile
        svc = TTSService()
        result = svc.synthesize("怀念父亲", VoiceProfile.GENTLE_MALE)
        assert isinstance(result.audio_bytes, (bytes, bytearray))
        assert len(result.audio_bytes) > 0
        assert result.provider == "mock"  # 其他 provider 不可用 → mock

    def test_voice_profile_enum_values(self):
        from deadman.multimodal.tts import VoiceProfile
        assert VoiceProfile.GENTLE_MALE.value == "gentle_male"
        assert VoiceProfile.GENTLE_FEMALE.value == "gentle_female"
        assert VoiceProfile.PROFESSIONAL_MALE.value == "professional_male"
        assert VoiceProfile.PROFESSIONAL_FEMALE.value == "professional_female"

    def test_speed_clamped(self):
        from deadman.multimodal.tts import TTSService, VoiceProfile
        svc = TTSService()
        # speed=10 应被夹到 2.0;mock 内部不报错即可
        result = svc.synthesize("测试", VoiceProfile.GENTLE_FEMALE, speed=10.0)
        assert len(result.audio_bytes) > 0

    def test_empty_text_returns_empty_audio(self):
        from deadman.multimodal.tts import TTSService, VoiceProfile
        svc = TTSService()
        result = svc.synthesize("", VoiceProfile.GENTLE_FEMALE)
        assert result.audio_bytes == b""
        assert result.provider == "empty"


# =====================================================================
# Vision tests
# =====================================================================


class TestVisionService:
    def test_instantiation(self):
        from deadman.multimodal.vision import VisionService
        svc = VisionService()
        assert "mock" in svc.list_providers()

    def test_describe_returns_string(self, sample_image):
        from deadman.multimodal.vision import VisionService
        svc = VisionService()
        text = svc.describe(sample_image, "描述这张图片")
        assert isinstance(text, str)
        assert text  # 非空

    def test_describe_memorial_prompt(self, sample_image):
        from deadman.multimodal.vision import VisionService
        svc = VisionService()
        text = svc.describe(sample_image, "用温暖语言描述逝者 memorial")
        # mock 对 memorial 关键词返回温暖描述
        assert "纪念" in text or "逝者" in text

    def test_extract_objects_returns_list(self, sample_image):
        from deadman.multimodal.vision import VisionService
        svc = VisionService()
        objs = svc.extract_objects(sample_image)
        assert isinstance(objs, list)
        assert len(objs) >= 1
        assert "label" in objs[0]
        assert "confidence" in objs[0]

    def test_describe_nonexistent_returns_empty(self, tmp_path):
        from deadman.multimodal.vision import VisionService
        svc = VisionService()
        assert svc.describe(tmp_path / "nope.png") == ""


# =====================================================================
# Image gen tests
# =====================================================================


class TestImageGenerator:
    def test_instantiation(self):
        from deadman.multimodal.image_gen import ImageGenerator
        gen = ImageGenerator()
        assert "mock" in gen.list_providers()

    def test_generate_returns_bytes(self):
        from deadman.multimodal.image_gen import ImageGenerator, ImageSize, ImageStyle
        gen = ImageGenerator()
        img_bytes = gen.generate("怀念父亲", ImageStyle.MEMORIAL_CARD, ImageSize.SQUARE_1024)
        assert isinstance(img_bytes, bytes)
        assert len(img_bytes) > 0

    def test_style_presets_contain_memorial_card(self):
        from deadman.multimodal.image_gen import STYLE_PRESETS, ImageStyle
        preset = STYLE_PRESETS[ImageStyle.MEMORIAL_CARD]
        assert "color_palette" in preset
        assert "prompt_template" in preset
        assert "negative_prompt" in preset
        # memorial_card 应包含 muted / respectful 色调
        palette = preset["color_palette"]
        assert any("cream" in c or "gray" in c or "gold" in c for c in palette)

    def test_get_style_preset(self):
        from deadman.multimodal.image_gen import ImageGenerator, ImageStyle
        gen = ImageGenerator()
        preset = gen.get_style_preset(ImageStyle.OBITUARY)
        assert preset["color_palette"]  # 非空

    def test_style_enum_values(self):
        from deadman.multimodal.image_gen import ImageStyle
        assert ImageStyle.MEMORIAL_CARD.value == "memorial_card"
        assert ImageStyle.OBITUARY.value == "obituary"
        assert ImageStyle.PORTRAIT.value == "portrait"
        assert ImageStyle.CONDOLENCE_CARD.value == "condolence_card"


# =====================================================================
# Storage tests
# =====================================================================


class TestMultimodalStorage:
    def test_store_and_retrieve(self, tmp_path):
        from deadman.multimodal.storage import MultimodalStorage
        store = MultimodalStorage(base_dir=tmp_path / "mm")
        meta = store.store(b"image-bytes", "image", "user_1", ext="png")
        assert meta.file_id
        assert meta.size == len(b"image-bytes")
        assert meta.file_type == "image"
        data = store.retrieve(meta.file_id, "user_1")
        assert data == b"image-bytes"

    def test_store_invalid_file_type_raises(self, tmp_path):
        from deadman.multimodal.storage import MultimodalStorage
        store = MultimodalStorage(base_dir=tmp_path / "mm")
        with pytest.raises(ValueError):
            store.store(b"x", "unknown_type", "user_1")

    def test_delete_file(self, tmp_path):
        from deadman.multimodal.storage import MultimodalStorage
        store = MultimodalStorage(base_dir=tmp_path / "mm")
        meta = store.store(b"audio-bytes", "audio", "user_1", ext="mp3")
        assert store.delete(meta.file_id, "user_1") is True
        # 删除后再 retrieve 返回 None
        assert store.retrieve(meta.file_id, "user_1") is None
        # 再次删除返回 False
        assert store.delete(meta.file_id, "user_1") is False

    def test_get_metadata(self, tmp_path):
        from deadman.multimodal.storage import MultimodalStorage
        store = MultimodalStorage(base_dir=tmp_path / "mm")
        meta = store.store(b"data", "generated", "user_2", ext="png")
        m = store.get_metadata(meta.file_id, "user_2")
        assert m is not None
        assert m.file_type == "generated"
        assert m.source_user == "user_2"

    def test_content_hash_is_sha256(self, tmp_path):
        import hashlib
        from deadman.multimodal.storage import MultimodalStorage
        store = MultimodalStorage(base_dir=tmp_path / "mm")
        data = b"hash-test"
        meta = store.store(data, "image", "u1", ext="png")
        assert meta.content_hash == hashlib.sha256(data).hexdigest()

    def test_temp_file_has_expires_at(self, tmp_path):
        from deadman.multimodal.storage import MultimodalStorage
        store = MultimodalStorage(base_dir=tmp_path / "mm")
        meta = store.store(b"temp-data", "temp", "u1", ext="bin")
        assert meta.expires_at is not None
        assert meta.expires_at > meta.created_at

    def test_permanent_file_has_no_expires_at(self, tmp_path):
        from deadman.multimodal.storage import MultimodalStorage
        store = MultimodalStorage(base_dir=tmp_path / "mm")
        meta = store.store(b"perm", "image", "u1", ext="png")
        assert meta.expires_at is None

    def test_list_files_filter_by_type(self, tmp_path):
        from deadman.multimodal.storage import MultimodalStorage
        store = MultimodalStorage(base_dir=tmp_path / "mm")
        store.store(b"a", "image", "u1", ext="png")
        store.store(b"b", "audio", "u1", ext="mp3")
        store.store(b"c", "image", "u1", ext="png")
        images = store.list_files("u1", file_type="image")
        assert len(images) == 2
        audios = store.list_files("u1", file_type="audio")
        assert len(audios) == 1

    def test_cleanup_expired_removes_temp(self, tmp_path):
        from deadman.multimodal.storage import MultimodalStorage
        store = MultimodalStorage(base_dir=tmp_path / "mm")
        meta = store.store(b"temp", "temp", "u1", ext="bin")
        # 手动改 expires_at 让它过期
        with store._lock:
            index = store._load_index("u1")
            index[meta.file_id].expires_at = 0.0  # 已过期
            store._save_index("u1", index)
        deleted = store.cleanup_expired("u1")
        assert deleted == 1
        assert store.retrieve(meta.file_id, "u1") is None

    def test_cleanup_does_not_remove_permanent(self, tmp_path):
        from deadman.multimodal.storage import MultimodalStorage
        store = MultimodalStorage(base_dir=tmp_path / "mm")
        meta = store.store(b"perm", "image", "u1", ext="png")
        deleted = store.cleanup_expired("u1")
        assert deleted == 0
        assert store.retrieve(meta.file_id, "u1") is not None

    def test_index_persistence_across_instances(self, tmp_path):
        from deadman.multimodal.storage import MultimodalStorage
        base = tmp_path / "mm"
        s1 = MultimodalStorage(base_dir=base)
        meta = s1.store(b"persist", "image", "u1", ext="png")
        # 重新创建实例,索引应能读到
        s2 = MultimodalStorage(base_dir=base)
        # 清缓存强制重读
        s2._index_cache.clear()
        m = s2.get_metadata(meta.file_id, "u1")
        assert m is not None
        assert m.file_id == meta.file_id


# =====================================================================
# Pipeline tests
# =====================================================================


class TestMultimodalPipeline:
    def test_instantiation_default_config(self):
        from deadman.multimodal.pipeline import MultimodalConfig, MultimodalPipeline
        pipe = MultimodalPipeline()
        assert isinstance(pipe.config, MultimodalConfig)
        assert pipe.is_enabled() is True

    def test_disabled_raises(self, monkeypatch, sample_image):
        from deadman.multimodal.pipeline import MultimodalDisabledError, MultimodalPipeline

        pipe = MultimodalPipeline()
        # 模拟 disabled
        monkeypatch.setenv("DEADMAN_MULTIMODAL_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        with pytest.raises(MultimodalDisabledError):
            pipe.ocr_extract(sample_image)
        # 恢复
        monkeypatch.setenv("DEADMAN_MULTIMODAL_ENABLED", "1")
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0

    def test_ocr_extract_via_pipeline(self, sample_image):
        from deadman.multimodal.ocr import DocType
        from deadman.multimodal.pipeline import MultimodalPipeline
        pipe = MultimodalPipeline()
        result = pipe.ocr_extract(sample_image, DocType.ID_CARD, user_id="u1")
        assert result.doc_type == DocType.ID_CARD

    def test_ocr_pii_redaction_integration(self, sample_image):
        """OCR 提取的 PII(身份证号)应被脱敏。"""
        from deadman.multimodal.ocr import DocType
        from deadman.multimodal.pipeline import MultimodalPipeline
        pipe = MultimodalPipeline()
        result = pipe.ocr_extract(sample_image, DocType.ID_CARD, user_id="u1")
        # mock OCR 返回 "姓名 张三 身份证号 110101199001011234"
        # 经 PII 检测后,身份证号应被部分脱敏
        assert "110101199001011234" not in result.text
        assert result.redacted is True

    def test_ocr_pii_redaction_can_be_disabled(self, sample_image):
        from deadman.multimodal.ocr import DocType
        from deadman.multimodal.pipeline import MultimodalConfig, MultimodalPipeline
        config = MultimodalConfig(pii_redact_ocr=False)
        pipe = MultimodalPipeline(config=config)
        result = pipe.ocr_extract(sample_image, DocType.ID_CARD, user_id="u1")
        # 关闭 PII 后,身份证号应原样存在
        assert "110101199001011234" in result.text
        assert result.redacted is False

    def test_asr_transcribe_via_pipeline(self, sample_audio):
        from deadman.multimodal.pipeline import MultimodalPipeline
        pipe = MultimodalPipeline()
        result = pipe.asr_transcribe(sample_audio, language="zh", user_id="u1")
        assert result.text

    def test_tts_synthesize_via_pipeline(self):
        from deadman.multimodal.pipeline import MultimodalPipeline
        from deadman.multimodal.tts import VoiceProfile
        pipe = MultimodalPipeline()
        result = pipe.tts_synthesize("悼文", VoiceProfile.GENTLE_MALE, user_id="u1")
        assert len(result.audio_bytes) > 0

    def test_vision_describe_via_pipeline(self, sample_image):
        from deadman.multimodal.pipeline import MultimodalPipeline
        pipe = MultimodalPipeline()
        text = pipe.vision_describe(sample_image, prompt="描述", user_id="u1")
        assert isinstance(text, str)

    def test_image_gen_via_pipeline(self):
        from deadman.multimodal.image_gen import ImageSize, ImageStyle
        from deadman.multimodal.pipeline import MultimodalPipeline
        pipe = MultimodalPipeline()
        img = pipe.image_gen_generate(
            "怀念父亲", ImageStyle.MEMORIAL_CARD, ImageSize.SQUARE_512, user_id="u1",
        )
        assert len(img) > 0

    def test_route_capability_ocr(self, sample_image):
        from deadman.multimodal.ocr import DocType
        from deadman.multimodal.pipeline import MultimodalPipeline
        pipe = MultimodalPipeline()
        result = pipe.route(
            "ocr", user_id="u1",
            image_path=sample_image, doc_type=DocType.OTHER,
        )
        assert result.text is not None

    def test_route_unknown_capability_raises(self):
        from deadman.multimodal.pipeline import MultimodalPipeline
        pipe = MultimodalPipeline()
        with pytest.raises(ValueError):
            pipe.route("unknown_cap")

    def test_route_capability_disabled_in_config(self, sample_image):
        from deadman.multimodal.pipeline import (
            MultimodalConfig, MultimodalDisabledError, MultimodalPipeline,
        )
        config = MultimodalConfig(enable_ocr=False)
        pipe = MultimodalPipeline(config=config)
        with pytest.raises(MultimodalDisabledError):
            pipe.ocr_extract(sample_image)

    def test_list_capabilities(self):
        from deadman.multimodal.pipeline import MultimodalConfig, MultimodalPipeline
        pipe = MultimodalPipeline(config=MultimodalConfig(
            enable_ocr=True, enable_asr=True, enable_tts=False,
            enable_vision=False, enable_image_gen=False,
        ))
        caps = pipe.list_capabilities()
        assert "ocr" in caps
        assert "asr" in caps
        assert "tts" not in caps
        assert "vision" not in caps

    def test_audit_log_recorded(self, sample_image):
        from deadman.multimodal.ocr import DocType
        from deadman.multimodal.pipeline import MultimodalPipeline
        pipe = MultimodalPipeline()
        pipe.ocr_extract(sample_image, DocType.OTHER, user_id="u_audit")
        audit = pipe.get_audit_log()
        assert len(audit) >= 1
        last = audit[-1]
        assert last["capability"] == "ocr"
        assert last["user_id"] == "u_audit"
        assert last["success"] is True

    def test_audit_log_records_failure(self, tmp_path):
        from deadman.multimodal.pipeline import MultimodalPipeline
        pipe = MultimodalPipeline()
        # 传一个不存在的文件,OCR service 不会抛但返回空 → success=True
        # 改为:让底层 service 抛错
        mock_ocr = MagicMock()
        mock_ocr.is_enabled.return_value = True
        mock_ocr.extract.side_effect = RuntimeError("boom")
        pipe.register_ocr(mock_ocr)
        with pytest.raises(RuntimeError):
            pipe.ocr_extract(tmp_path / "x.png")
        audit = pipe.get_audit_log()
        assert any(e["success"] is False and e["error"] == "boom" for e in audit)

    def test_budget_tracking_integration(self, sample_image):
        """验证 pipeline 调用 BudgetCoordinator 扣 budget。"""
        from deadman.multimodal.ocr import DocType
        from deadman.multimodal.pipeline import MultimodalPipeline
        from deadman.infrastructure.defense.budget_coordinator import (
            BudgetDimension, BudgetScope, get_budget_coordinator,
        )
        pipe = MultimodalPipeline()
        pipe.ocr_extract(sample_image, DocType.OTHER, user_id="u_budget")
        bc = get_budget_coordinator()
        status = bc.check(BudgetScope.USER, "u_budget", BudgetDimension.LLM_TOKENS)
        # 100 token 被 ocr 扣减
        assert status["used"] >= 100


# =====================================================================
# Disabled state tests
# =====================================================================


class TestDisabledState:
    def test_ocr_raises_when_disabled(self, monkeypatch, sample_image):
        from deadman.multimodal.ocr import DocType, OCRService
        from deadman.multimodal.pipeline import MultimodalDisabledError

        monkeypatch.setenv("DEADMAN_MULTIMODAL_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0

        svc = OCRService()
        with pytest.raises(MultimodalDisabledError):
            svc.extract(sample_image, DocType.OTHER)

        # 恢复
        monkeypatch.setenv("DEADMAN_MULTIMODAL_ENABLED", "1")
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0

    def test_tts_raises_when_disabled(self, monkeypatch):
        from deadman.multimodal.pipeline import MultimodalDisabledError
        from deadman.multimodal.tts import TTSService, VoiceProfile

        monkeypatch.setenv("DEADMAN_MULTIMODAL_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0

        svc = TTSService()
        with pytest.raises(MultimodalDisabledError):
            svc.synthesize("x", VoiceProfile.GENTLE_MALE)

        monkeypatch.setenv("DEADMAN_MULTIMODAL_ENABLED", "1")
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0

    def test_storage_raises_when_disabled(self, monkeypatch, tmp_path):
        from deadman.multimodal.pipeline import MultimodalDisabledError
        from deadman.multimodal.storage import MultimodalStorage

        monkeypatch.setenv("DEADMAN_MULTIMODAL_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0

        store = MultimodalStorage(base_dir=tmp_path / "mm")
        with pytest.raises(MultimodalDisabledError):
            store.store(b"x", "image", "u1")

        monkeypatch.setenv("DEADMAN_MULTIMODAL_ENABLED", "1")
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0


# =====================================================================
# Module exports tests
# =====================================================================


class TestModuleExports:
    def test_required_exports_available(self):
        import deadman.multimodal as mm
        required = [
            "OCRService", "ASRService", "TTSService", "VisionService",
            "ImageGenerator", "MultimodalPipeline", "MultimodalConfig",
            "get_multimodal_pipeline", "MultimodalDisabledError",
        ]
        for name in required:
            assert hasattr(mm, name), f"Missing export: {name}"

    def test_get_multimodal_pipeline_singleton(self):
        from deadman.multimodal.pipeline import (
            get_multimodal_pipeline, reset_multimodal_pipeline,
        )
        p1 = get_multimodal_pipeline()
        p2 = get_multimodal_pipeline()
        assert p1 is p2
        reset_multimodal_pipeline()
        p3 = get_multimodal_pipeline()
        assert p3 is not p1
