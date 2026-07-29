"""DocumentExtractor - AI 文档提取

参考 Trust & Will 文档提取功能：
    - 用户上传遗嘱/保险单/房产证/银行流水等
    - AI 提取关键字段生成摘要
    - 文件级 PII 脱敏（账号/身份证号/手机号/邮箱）
    - 标注 confidence（不确定字段不输出确定结论）

遵守：
    - rules/integrity-framework.md：不确定字段标 confidence < 0.7，不编造
    - rules/legal-compliance-framework.md 第五章 PIPL：
        * 文件级 PII 脱敏后再喂 LLM
        * 用户上传的原文不进地域知识库
    - rules/retrieval-guardrails.md：摘要含 confidence 标记
    - rules/service-boundary-framework.md：不替代律师审阅

不引入新 pip 依赖：
    - PDF 不用 PyPDF2（仅用 stdlib 简单解析；复杂 PDF 标 unsupported）
    - OCR 不用 pytesseract（图片标 needs_ocr）
    - LLM 调用走 deadman.llm.llm_client；不可用时降级 confidence=0.3
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..vault.store import VaultStore

logger = logging.getLogger(__name__)


# =====================================================================
# 文档类型常量
# =====================================================================
DOC_TYPE_WILL = "will"
DOC_TYPE_TRUST = "trust"
DOC_TYPE_INSURANCE = "insurance"
DOC_TYPE_PROPERTY = "property"
DOC_TYPE_BANK_STATEMENT = "bank_statement"
DOC_TYPE_ID_CARD = "id_card"
DOC_TYPE_OTHER = "other"

_VALID_DOC_TYPES = {
    DOC_TYPE_WILL, DOC_TYPE_TRUST, DOC_TYPE_INSURANCE,
    DOC_TYPE_PROPERTY, DOC_TYPE_BANK_STATEMENT,
    DOC_TYPE_ID_CARD, DOC_TYPE_OTHER,
}


# =====================================================================
# ExtractedDocument 数据结构
# =====================================================================
@dataclass
class ExtractedDocument:
    """提取的文档摘要

    key_fields 字段因文档类型而异：
        - will: {testator, beneficiaries, executor, witnesses, date, notarized}
        - insurance: {insurer, policy_number_masked, insured, beneficiary,
                      sum_assured_masked, valid_until}
        - property: {address_masked, ownership_type, co_owners, mortgage}
        - bank_statement: {bank, account_masked, balance_masked, as_of_date}

    source_text_masked 是 PII 脱敏后的原文（用于用户校对），
    不含完整身份证号 / 银行账号 / 手机号 / 邮箱明文。
    """
    doc_id: str
    owner_user_id: str
    filename: str
    file_type: str  # pdf / image / txt / docx
    file_size: int
    uploaded_at: datetime
    doc_type: str
    summary: str
    key_fields: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    source_text_masked: str = ""
    # 关联保险库条目 id（加密原文存放在 vault 中）
    vault_item_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["uploaded_at"] = self.uploaded_at.isoformat()
        return d


# =====================================================================
# DocumentExtractor
# =====================================================================
class DocumentExtractor:
    """AI 文档提取 - 上传 + 解析 + PII 脱敏 + LLM 摘要"""

    # LLM 不可用时的固定 confidence
    LLM_UNAVAILABLE_CONFIDENCE = 0.3

    # 低于此阈值视为不确定字段（integrity-framework 要求标注）
    LOW_CONFIDENCE_THRESHOLD = 0.7

    def __init__(self, vault: VaultStore | None = None) -> None:
        self.vault = vault or VaultStore()
        # 文档元数据索引（不含 source_text）
        # 路径：~/.deadman/documents/{user_id}/index.json
        if hasattr(self.vault, "data_dir"):
            self.data_dir: Path = self.vault.data_dir.parent / "documents"
        else:
            self.data_dir = Path.home() / ".deadman" / "documents"
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("DocumentExtractor 创建数据目录失败 %s: %s", self.data_dir, exc)

    # ==================================================================
    # 主流程
    # ==================================================================
    async def extract(
        self,
        owner_user_id: str,
        filename: str,
        content: bytes,
        doc_type_hint: str | None = None,
    ) -> ExtractedDocument:
        """提取文档

        步骤：
            1. 文件存入 vault（加密）
            2. 提取文本（PDF 用 stdlib；图片 OCR 不可用则跳过；txt 直接读）
            3. PII 脱敏
            4. 调 LLM 生成摘要 + 关键字段
            5. 标 confidence
        """
        doc_id = f"doc-{uuid.uuid4().hex[:12]}"
        file_type = self._detect_file_type(filename, content)
        file_size = len(content)

        # 1. 加密存入 vault（type=document，受益人默认空，由用户后续指定）
        vault_item = self.vault.add_item(
            owner_user_id=owner_user_id,
            type="document",
            title=filename,
            content=content,
            beneficiary_user_ids=[],
            delivery_trigger=VaultStore.TRIGGER_MANUAL
            if hasattr(VaultStore, "TRIGGER_MANUAL")
            else "manual",
            metadata={"doc_id": doc_id, "file_type": file_type},
        )

        # 2. 提取文本
        raw_text = self._extract_text(content, file_type)

        # 3. PII 脱敏
        masked_text = self._mask_pii_in_text(raw_text)

        # 4. 推断文档类型
        doc_type = doc_type_hint if doc_type_hint in _VALID_DOC_TYPES else self._guess_doc_type(filename, masked_text)

        # 5. LLM 提取
        llm_result = await self._llm_extract(masked_text, doc_type)

        doc = ExtractedDocument(
            doc_id=doc_id,
            owner_user_id=owner_user_id,
            filename=filename,
            file_type=file_type,
            file_size=file_size,
            uploaded_at=datetime.now(timezone.utc).replace(tzinfo=None),
            doc_type=doc_type,
            summary=llm_result.get("summary", ""),
            key_fields=llm_result.get("key_fields", {}) or {},
            confidence=float(llm_result.get("confidence", 0.0)),
            source_text_masked=masked_text,
            vault_item_id=vault_item.item_id,
        )

        # 写入索引
        self._save_index(doc)
        return doc

    # ==================================================================
    # 文件类型识别
    # ==================================================================
    @staticmethod
    def _detect_file_type(filename: str, content: bytes) -> str:
        name = filename.lower()
        if name.endswith(".pdf") or content[:4] == b"%PDF":
            return "pdf"
        if name.endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")):
            return "image"
        if name.endswith((".txt", ".md", ".text")):
            return "txt"
        if name.endswith((".docx",)):
            return "docx"
        if name.endswith((".doc",)):
            return "doc"
        # 兜底：按魔数判断
        if content[:8] == b"\x89PNG\r\n\x1a\n":
            return "image"
        if content[:3] == b"\xff\xd8\xff":
            return "image"
        return "unknown"

    # ==================================================================
    # 文本提取
    # ==================================================================
    def _extract_text(self, content: bytes, file_type: str) -> str:
        """提取文本

        - txt: 直接 decode（utf-8 优先，失败回退 gbk/llatin-1）
        - pdf: 用 stdlib 简单解析（仅尝试找 BT/ET 文本块）；
               复杂 PDF 标记 "[unsupported_pdf_format]"
        - docx: 标记 "[unsupported_docx_format]"
        - image: 标记 "[needs_ocr]"
        - 其他: 标记 "[unsupported_format]"
        """
        if file_type == "txt":
            for enc in ("utf-8", "gbk", "latin-1"):
                try:
                    return content.decode(enc)
                except UnicodeDecodeError:
                    continue
            return ""
        if file_type == "pdf":
            return self._extract_pdf_text(content)
        if file_type == "image":
            return "[needs_ocr]"
        if file_type in ("docx", "doc"):
            return "[unsupported_docx_format]"
        return "[unsupported_format]"

    @staticmethod
    def _extract_pdf_text(content: bytes) -> str:
        """极简 PDF 文本提取 - 仅解析 BT...ET 块中的 Tj/TJ 操作符

        适用于未压缩、无加密的简单 PDF。
        复杂 PDF（压缩流、加密、字体子集）会返回 "[unsupported_pdf_format]" 提示。
        """
        try:
            raw = content.decode("latin-1")  # PDF 内部用 latin-1
        except UnicodeDecodeError:
            return "[unsupported_pdf_format]"
        # 检测是否含 FlateDecode 压缩流（这些我们处理不了）
        if "FlateDecode" in raw or "/Filter" in raw:
            # 仅当所有文本块都被压缩时才标 unsupported
            # 简单 PDF 通常文本不在流里
            pass
        texts: list[str] = []
        # 匹配 (...) Tj 形式
        for m in re.finditer(r"\((.*?)\)\s*Tj", raw, re.DOTALL):
            texts.append(m.group(1))
        # 匹配 [...] TJ 数组形式
        for m in re.finditer(r"\[(.*?)\]\s*TJ", raw, re.DOTALL):
            # 提取 (...) 中的字符串
            inner = re.findall(r"\((.*?)\)", m.group(1))
            if inner:
                texts.append("".join(inner))
        if not texts:
            # 没匹配到任何文本，可能整页用了 FlateDecode 压缩
            return "[unsupported_pdf_format]"
        return "\n".join(texts)

    # ==================================================================
    # PII 脱敏
    # ==================================================================
    def _mask_pii_in_text(self, text: str) -> str:
        """文本级 PII 脱敏

        - 身份证号 18 位 → 前 6 后 4 中间 *
        - 手机号 11 位 → 前 3 后 4 中间 *
        - 银行账号 16-19 位 → 前 4 后 4 中间 *
        - 邮箱 → 前 1 后域名 *

        注：脱敏后保留长度信息以辅助用户校对，但不还原完整号码。
        """
        if not text:
            return ""

        # 1. 身份证号 18 位（前 6 位地区码 + 8 位生日 + 3 位序号 + 1 位校验）
        #    不严格要求最后一位是 X/数字，避免漏脱敏
        text = re.sub(
            r"\b(\d{6})\d{8}(\d{3}[\dXx])\b",
            lambda m: f"{m.group(1)}********{m.group(2)}",
            text,
        )

        # 2. 手机号 11 位（1 开头）
        text = re.sub(
            r"\b(1[3-9]\d)\d{4}(\d{4})\b",
            lambda m: f"{m.group(1)}****{m.group(2)}",
            text,
        )

        # 3. 银行账号 16-19 位（连续数字）
        def _mask_bank(m: re.Match) -> str:
            digits = m.group(0)
            return f"{digits[:4]}{'*' * (len(digits) - 8)}{digits[-4:]}"

        text = re.sub(r"\b\d{16,19}\b", _mask_bank, text)

        # 4. 邮箱（前 1 字符 + *** + @域名）
        text = re.sub(
            r"\b([a-zA-Z0-9._%+-])[a-zA-Z0-9._%+-]*@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b",
            lambda m: f"{m.group(1)}***@{m.group(2)}",
            text,
        )

        return text

    # ==================================================================
    # LLM 提取
    # ==================================================================
    async def _llm_extract(self, masked_text: str, doc_type: str) -> dict[str, Any]:
        """调 LLM 提取关键字段

        返回 {summary, key_fields, confidence}
        LLM 不可用 / 调用失败 / 文本过短时返回 confidence=0.3
        """
        # 文本不可读或为空 → 直接低 confidence
        if not masked_text or masked_text.startswith("[needs_ocr]") or masked_text.startswith("[unsupported_"):
            return {
                "summary": f"文档未能提取有效文本（{masked_text}），需用户手动填写关键字段。",
                "key_fields": {},
                "confidence": self.LLM_UNAVAILABLE_CONFIDENCE,
            }

        try:
            from ..llm import llm_client
        except Exception as exc:
            logger.warning("DocumentExtractor: 无法导入 llm_client: %s", exc)
            return self._llm_unavailable_result()

        if not llm_client.api_key:
            return self._llm_unavailable_result()

        prompt = self._build_prompt(masked_text, doc_type)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是文档解析助手，专注于从用户上传的法律/财务/医疗文档中提取关键字段。"
                    "遵守："
                    "1) 不编造，文本中未提及的字段不要补全；"
                    "2) 对每个字段给 0-1 的 confidence；"
                    "3) 输出严格 JSON 格式：{\"summary\": str, \"key_fields\": dict, \"confidence\": float}；"
                    "4) 不替代律师/会计师审阅；"
                    "5) 若文本是脱敏占位符（如 ***），相关字段 confidence 降至 0.3 以下。"
                ),
            },
            {"role": "user", "content": prompt},
        ]

        try:
            resp = await llm_client.chat(messages, temperature=0.1, max_tokens=1024)
        except Exception as exc:
            logger.warning("DocumentExtractor: LLM 调用失败: %s", exc)
            return self._llm_unavailable_result()

        return self._parse_llm_response(resp)

    def _llm_unavailable_result(self) -> dict[str, Any]:
        return {
            "summary": "LLM 不可用，仅存储原文。请稍后重试或手动填写关键字段。",
            "key_fields": {},
            "confidence": self.LLM_UNAVAILABLE_CONFIDENCE,
        }

    @staticmethod
    def _build_prompt(masked_text: str, doc_type: str) -> str:
        type_hint = {
            DOC_TYPE_WILL: "遗嘱（will）：提取 testator/beneficiaries/executor/witnesses/date/notarized",
            DOC_TYPE_TRUST: "信托（trust）：提取 settlor/trustee/beneficiaries/assets/date",
            DOC_TYPE_INSURANCE: "保险单（insurance）：提取 insurer/policy_number_masked/insured/beneficiary/sum_assured_masked/valid_until",
            DOC_TYPE_PROPERTY: "房产证（property）：提取 address_masked/ownership_type/co_owners/mortgage",
            DOC_TYPE_BANK_STATEMENT: "银行流水（bank_statement）：提取 bank/account_masked/balance_masked/as_of_date",
            DOC_TYPE_ID_CARD: "身份证件（id_card）：仅提取签发机关、有效期；不输出完整身份证号",
            DOC_TYPE_OTHER: "通用文档：提取关键日期、当事人、金额、机构",
        }.get(doc_type, "通用文档：提取关键字段")

        # 截断超长文本（避免 token 浪费）
        snippet = masked_text[:4000]
        if len(masked_text) > 4000:
            snippet += "\n...(后续内容省略)"

        return (
            f"文档类型提示：{doc_type}\n"
            f"应提取字段：{type_hint}\n\n"
            f"以下是 PII 脱敏后的文档原文：\n```\n{snippet}\n```\n\n"
            "请输出 JSON：{\"summary\": str, \"key_fields\": dict, \"confidence\": float}。"
            "summary 用 2-3 句话概括；key_fields 为字段名到值的映射（未提及的字段不要编造）；"
            "confidence 是整体提取可信度（0-1）。"
        )

    def _parse_llm_response(self, resp: str) -> dict[str, Any]:
        """从 LLM 响应中解析 JSON（容错）"""
        if not resp:
            return self._llm_unavailable_result()
        # 尝试提取 JSON 块
        candidate = resp.strip()
        # 去掉 markdown code fence
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            if len(lines) >= 2:
                lines = lines[1:]  # 去掉首行 ```
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                candidate = "\n".join(lines).strip()
        # 尝试直接 json.loads
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            # 尝试找第一个 { ... } 块
            m = re.search(r"\{.*\}", candidate, re.DOTALL)
            if not m:
                logger.warning("DocumentExtractor: LLM 响应非 JSON: %s", resp[:200])
                return self._llm_unavailable_result()
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return self._llm_unavailable_result()

        summary = str(data.get("summary", "")).strip()
        key_fields = data.get("key_fields", {}) or {}
        if not isinstance(key_fields, dict):
            key_fields = {}
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        return {"summary": summary, "key_fields": key_fields, "confidence": confidence}

    # ==================================================================
    # 文档类型推断（hint 缺失时）
    # ==================================================================
    @staticmethod
    def _guess_doc_type(filename: str, masked_text: str) -> str:
        name = filename.lower()
        if "will" in name or "遗嘱" in masked_text or "遗嘱" in name:
            return DOC_TYPE_WILL
        if "trust" in name or "信托" in masked_text:
            return DOC_TYPE_TRUST
        if "insurance" in name or "保险" in masked_text or "保单" in masked_text:
            return DOC_TYPE_INSURANCE
        if "property" in name or "房产" in masked_text or "不动产权" in masked_text:
            return DOC_TYPE_PROPERTY
        if "bank" in name or "银行" in masked_text or "流水" in name:
            return DOC_TYPE_BANK_STATEMENT
        if "id" in name or "身份证" in masked_text:
            return DOC_TYPE_ID_CARD
        return DOC_TYPE_OTHER

    # ==================================================================
    # 索引读写
    # ==================================================================
    def _index_file(self, user_id: str) -> Path:
        return self.data_dir / user_id / "index.json"

    def _read_index(self, user_id: str) -> dict[str, dict[str, Any]]:
        path = self._index_file(user_id)
        if not path.exists():
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("DocumentExtractor 读取索引失败 %s: %s", path, exc)
            return {}

    def _write_index(self, user_id: str, index: dict[str, dict[str, Any]]) -> None:
        path = self._index_file(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("DocumentExtractor 写入索引失败 %s: %s", path, exc)

    def _save_index(self, doc: ExtractedDocument) -> None:
        index = self._read_index(doc.owner_user_id)
        # 不存 source_text_masked 以避免脱敏原文冗余存储；
        # 但保留 summary/key_fields/confidence/file_size/file_type 等
        entry = doc.to_dict()
        entry.pop("source_text_masked", None)
        index[doc.doc_id] = entry
        self._write_index(doc.owner_user_id, index)

    # ==================================================================
    # 查询接口
    # ==================================================================
    def list_my_documents(self, owner_user_id: str) -> list[ExtractedDocument]:
        """列出我上传的所有文档（不含 source_text_masked）"""
        index = self._read_index(owner_user_id)
        results: list[ExtractedDocument] = []
        for entry in index.values():
            results.append(self._entry_to_doc(entry))
        return results

    def get_document(
        self, doc_id: str, requester_user_id: str
    ) -> ExtractedDocument | None:
        """获取文档详情

        权限：
            - 仅 owner 可获取（vault 文件本身有受益人权限控制，
              但提取结果默认仅 owner 可见，避免脱敏不彻底时泄露）
        """
        index = self._read_index(requester_user_id)
        entry = index.get(doc_id)
        if entry:
            return self._entry_to_doc(entry)
        # 否则检查所有用户目录（防止 owner_id 不一致）
        for user_dir in self.data_dir.iterdir():
            if not user_dir.is_dir():
                continue
            owner_id = user_dir.name
            if owner_id == requester_user_id:
                continue
            other_index = self._read_index(owner_id)
            entry = other_index.get(doc_id)
            if entry and entry.get("owner_user_id") == requester_user_id:
                return self._entry_to_doc(entry)
        return None

    def delete_document(self, doc_id: str, owner_user_id: str) -> bool:
        """删除文档（仅 owner 可删）

        会同步删除 vault 中关联的加密原文。
        """
        index = self._read_index(owner_user_id)
        entry = index.get(doc_id)
        if not entry:
            return False
        vault_item_id = entry.get("vault_item_id")
        del index[doc_id]
        self._write_index(owner_user_id, index)
        # 同步删 vault 中的原文
        if vault_item_id:
            try:
                self.vault.delete_item(vault_item_id, owner_user_id)
            except Exception as exc:
                logger.warning("DocumentExtractor: 同步删除 vault 条目失败: %s", exc)
        return True

    @staticmethod
    def _entry_to_doc(entry: dict[str, Any]) -> ExtractedDocument:
        """索引条目转 ExtractedDocument（无 source_text_masked）"""
        def _parse_dt(v: Any) -> datetime:
            if not v:
                return datetime.now(timezone.utc).replace(tzinfo=None)
            try:
                return datetime.fromisoformat(v)
            except (TypeError, ValueError):
                return datetime.now(timezone.utc).replace(tzinfo=None)

        return ExtractedDocument(
            doc_id=entry["doc_id"],
            owner_user_id=entry["owner_user_id"],
            filename=entry.get("filename", ""),
            file_type=entry.get("file_type", ""),
            file_size=int(entry.get("file_size", 0)),
            uploaded_at=_parse_dt(entry.get("uploaded_at")),
            doc_type=entry.get("doc_type", DOC_TYPE_OTHER),
            summary=entry.get("summary", ""),
            key_fields=entry.get("key_fields", {}) or {},
            confidence=float(entry.get("confidence", 0.0)),
            source_text_masked="",  # 索引不存原文
            vault_item_id=entry.get("vault_item_id"),
        )
