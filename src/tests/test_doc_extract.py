"""测试 deadman.doc_extract.extractor - AI 文档提取

覆盖：
    - txt 文件提取文本
    - PDF 标 unsupported 不抛
    - 身份证号脱敏
    - 手机号脱敏
    - 银行账号脱敏
    - 邮箱脱敏
    - 无 LLM 时 confidence=0.3
    - 列出我的文档
    - 无权限返回 None

测试隔离：每个测试用 tmp_path 独立目录。
LLM 全部走 mock（不真实调用）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from deadman.doc_extract.extractor import (
    DOC_TYPE_OTHER,
    DOC_TYPE_WILL,
    DocumentExtractor,
    ExtractedDocument,
)
from deadman.vault.store import VaultStore


# =====================================================================
# 辅助：构造独立 extractor
# =====================================================================
def _make_extractor(tmp_path: Path) -> DocumentExtractor:
    vault = VaultStore(data_dir=tmp_path / "vault")
    return DocumentExtractor(vault=vault)


def _make_mock_llm(
    resp_text: str = '{"summary":"遗嘱摘要","key_fields":{"testator":"张三"},"confidence":0.85}',
):
    """构造一个 mock llm_client，chat 是 AsyncMock"""
    client = MagicMock()
    client.api_key = "test-key-not-real"
    client.chat = AsyncMock(return_value=resp_text)
    return client


# =====================================================================
# 1. txt 文件提取文本
# =====================================================================
def test_extract_txt_file(tmp_path: Path):
    extractor = _make_extractor(tmp_path)
    text = "这是一份遗嘱。立遗嘱人：张三。受益人：张小明。"
    content = text.encode("utf-8")

    # 注入 mock LLM
    import deadman.llm as llm_module

    mock_llm = _make_mock_llm()
    old_client = llm_module.llm_client
    llm_module.llm_client = mock_llm
    try:
        doc = asyncio.run(
            extractor.extract(
                owner_user_id="u-owner",
                filename="will.txt",
                content=content,
                doc_type_hint=DOC_TYPE_WILL,
            )
        )
    finally:
        llm_module.llm_client = old_client

    assert isinstance(doc, ExtractedDocument)
    assert doc.filename == "will.txt"
    assert doc.file_type == "txt"
    assert doc.file_size == len(content)
    assert doc.doc_type == DOC_TYPE_WILL
    # source_text_masked 应保留中文文本
    assert "遗嘱" in doc.source_text_masked
    # summary 来自 mock LLM
    assert doc.summary == "遗嘱摘要"
    assert doc.key_fields.get("testator") == "张三"
    assert doc.confidence == pytest.approx(0.85)


# =====================================================================
# 2. PDF 标 unsupported 不抛
# =====================================================================
def test_extract_pdf_unsupported_graceful(tmp_path: Path):
    extractor = _make_extractor(tmp_path)
    # 一个非常简单的 PDF 头，但内容无任何 Tj 文本块
    fake_pdf = b"%PDF-1.4\n%binary garbage\n%%EOF"

    # LLM mock 返回带低 confidence 的 JSON
    import deadman.llm as llm_module

    mock_llm = _make_mock_llm('{"summary":"PDF 格式不支持","key_fields":{},"confidence":0.3}')
    old_client = llm_module.llm_client
    llm_module.llm_client = mock_llm
    try:
        # 不应抛异常
        doc = asyncio.run(
            extractor.extract(
                owner_user_id="u-owner",
                filename="scan.pdf",
                content=fake_pdf,
            )
        )
    finally:
        llm_module.llm_client = old_client

    assert doc.file_type == "pdf"
    # 应该是 unsupported 标记
    assert (
        "unsupported_pdf_format" in doc.source_text_masked or "needs_ocr" in doc.source_text_masked
    )
    # confidence 应较低
    assert doc.confidence <= 0.5


# =====================================================================
# 3. 身份证号脱敏
# =====================================================================
def test_mask_pii_id_card():
    extractor = _make_extractor(Path("/tmp"))
    text = "身份证：110101199001011234"
    masked = extractor._mask_pii_in_text(text)
    assert "110101199001011234" not in masked
    assert "110101" in masked  # 前 6 位保留
    assert "1234" in masked  # 后 4 位保留
    assert "********" in masked


# =====================================================================
# 4. 手机号脱敏
# =====================================================================
def test_mask_pii_phone():
    extractor = _make_extractor(Path("/tmp"))
    text = "联系电话：13812345678"
    masked = extractor._mask_pii_in_text(text)
    assert "13812345678" not in masked
    assert "138" in masked
    assert "5678" in masked
    assert "****" in masked


# =====================================================================
# 5. 银行账号脱敏
# =====================================================================
def test_mask_pii_bank_account():
    extractor = _make_extractor(Path("/tmp"))
    text = "账号：6222021234567890123"
    masked = extractor._mask_pii_in_text(text)
    assert "6222021234567890123" not in masked
    assert "6222" in masked  # 前 4 位
    assert "0123" in masked  # 后 4 位
    assert "*" in masked


# =====================================================================
# 6. 邮箱脱敏
# =====================================================================
def test_mask_pii_email():
    extractor = _make_extractor(Path("/tmp"))
    text = "邮箱：zhangsan@example.com"
    masked = extractor._mask_pii_in_text(text)
    assert "zhangsan@example.com" not in masked
    assert "zhangsan" not in masked
    assert "@example.com" in masked
    assert "***" in masked


# =====================================================================
# 7. 无 LLM 时 confidence=0.3
# =====================================================================
def test_llm_extract_without_key_returns_low_confidence(tmp_path: Path):
    extractor = _make_extractor(tmp_path)
    text = "这是一份遗嘱。立遗嘱人：张三。"

    # 注入无 api_key 的 mock LLM
    import deadman.llm as llm_module

    mock_llm = MagicMock()
    mock_llm.api_key = ""  # 空 key
    old_client = llm_module.llm_client
    llm_module.llm_client = mock_llm
    try:
        result = asyncio.run(extractor._llm_extract(text, DOC_TYPE_WILL))
    finally:
        llm_module.llm_client = old_client

    assert result["confidence"] == pytest.approx(0.3)
    assert "LLM 不可用" in result["summary"] or "仅存储原文" in result["summary"]
    assert result["key_fields"] == {}


# =====================================================================
# 8. 列出我的文档
# =====================================================================
def test_list_my_documents(tmp_path: Path):
    extractor = _make_extractor(tmp_path)

    import deadman.llm as llm_module

    mock_llm = _make_mock_llm()
    old_client = llm_module.llm_client
    llm_module.llm_client = mock_llm
    try:
        asyncio.run(extractor.extract("u-owner", "a.txt", b"doc A", DOC_TYPE_WILL))
        asyncio.run(extractor.extract("u-owner", "b.txt", b"doc B", DOC_TYPE_OTHER))
        asyncio.run(extractor.extract("u-other", "c.txt", b"doc C", DOC_TYPE_OTHER))
    finally:
        llm_module.llm_client = old_client

    docs = extractor.list_my_documents("u-owner")
    assert len(docs) == 2
    names = {d.filename for d in docs}
    assert names == {"a.txt", "b.txt"}


# =====================================================================
# 9. 无权限返回 None
# =====================================================================
def test_get_document_unauthorized(tmp_path: Path):
    extractor = _make_extractor(tmp_path)

    import deadman.llm as llm_module

    mock_llm = _make_mock_llm()
    old_client = llm_module.llm_client
    llm_module.llm_client = mock_llm
    try:
        doc = asyncio.run(extractor.extract("u-owner", "a.txt", b"doc A", DOC_TYPE_WILL))
    finally:
        llm_module.llm_client = old_client

    # owner 能拿到
    fetched = extractor.get_document(doc.doc_id, "u-owner")
    assert fetched is not None
    assert fetched.doc_id == doc.doc_id
    # 陌生人拿不到
    assert extractor.get_document(doc.doc_id, "u-stranger") is None
