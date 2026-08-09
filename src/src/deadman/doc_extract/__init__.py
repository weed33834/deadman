"""AI 文档提取（Phase 12）

参考 Trust & Will 的文档提取功能 + GoodTrust 资产跟踪 + Codex Vitae 自动整理。
用户上传遗嘱/保险单/房产证/银行流水等，AI 提取关键字段生成摘要。

遵守：
    - rules/integrity-framework.md：不确定字段标 confidence < 0.7
    - rules/legal-compliance-framework.md 第五章 PIPL：文件级 PII 脱敏
    - rules/retrieval-guardrails.md：摘要含 confidence 标记
    - rules/service-boundary-framework.md：不替代律师审阅

子模块：
    - extractor: DocumentExtractor / ExtractedDocument
"""

from __future__ import annotations

from .extractor import DocumentExtractor, ExtractedDocument

__all__ = ["DocumentExtractor", "ExtractedDocument"]
