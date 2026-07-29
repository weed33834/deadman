"""P8.7 Supervised Fine-Tuning (SFT) 数据集构建器。

用途:
    - 收集 deadman 5 大领域(法律 / 医疗 / 情感 / 财务 / 通用)的高质量 SFT 样本
    - PII 脱敏入库(强制走 defense.pii_guard.PIIRedactor)
    - 数据集质量治理:质量分过滤 / 任务类型过滤 / 去重 / 类别均衡
    - 多格式导出:jsonl / csv / alpaca / sharegpt(对接主流训练框架)

设计原则:
    - Lineage(数据血缘):每条样本带 source(user_feedback / auto_generated / manual),
      便于合规审计与可删除权(PIPL/GDPR)
    - PII 强制脱敏:加入前 PIIRedactor.redact,默认 PARTIAL 策略
    - 原子写 + 线程安全:同 dpo_trainer
    - 无外部依赖(标准库 csv / json)

不依赖 torch / transformers / datasets。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import threading
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..infrastructure.defense.pii_guard import PIIRedactor, get_pii_redactor
from ..infrastructure.multi_tenant import resolve_data_path

logger = logging.getLogger(__name__)


# =====================================================================
# 任务类型 - deadman 5 大领域
# =====================================================================
class TaskType(str, Enum):
    """SFT 样本任务类型(对应 deadman 业务领域)。"""

    LEGAL = "legal"            # 法律咨询(遗嘱 / 继承 / 监护)
    MEDICAL = "medical"        # 医疗(临终关怀 / 疼痛管理)
    EMOTIONAL = "emotional"    # 情感支持(哀伤辅导)
    FINANCIAL = "financial"    # 财务(遗产分配 / 税务)
    GENERAL = "general"        # 通用(平台导航 / FAQ)


class SFTSource(str, Enum):
    """样本来源(lineage)。"""

    USER_FEEDBACK = "user_feedback"     # 用户反馈高赞回复
    AUTO_GENERATED = "auto_generated"   # 自动生成(模板 / LLM 增强)
    MANUAL = "manual"                   # 人工编写(domain expert)
    REFLEXION = "reflexion"             # Reflexion 修正后的回复


class ExportFormat(str, Enum):
    """导出格式。"""

    JSONL = "jsonl"
    CSV = "csv"
    ALPACA = "alpaca"     # {"instruction", "input", "output"}
    SHAREGPT = "sharegpt"  # {"conversations": [{"from": "human"/"gpt", "value": ...}]}


# =====================================================================
# 数据类
# =====================================================================
@dataclass
class SFTExample:
    """单条 SFT 样本。

    Attributes:
        prompt: 输入(用户问题)
        completion: 输出(期望回复)
        task_type: 任务类型(5 大领域)
        quality_score: 质量分(0-1,人工或 LLM-as-Judge 评分)
        source: 来源(lineage)
        timestamp: 收集时间
        user_id: 提供者(用于追溯 / 删除)
        redacted: 是否已 PII 脱敏
        prompt_hash: prompt 哈希(用于去重)
    """

    prompt: str
    completion: str
    task_type: TaskType = TaskType.GENERAL
    quality_score: float = 0.5
    source: SFTSource = SFTSource.MANUAL
    timestamp: float = field(default_factory=time.time)
    user_id: str = ""
    redacted: bool = False
    prompt_hash: str = ""

    def __post_init__(self) -> None:
        if not self.prompt_hash:
            self.prompt_hash = _hash_text(self.prompt)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["task_type"] = self.task_type.value
        d["source"] = self.source.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SFTExample:
        tt = data.get("task_type", TaskType.GENERAL.value)
        if isinstance(tt, str):
            try:
                tt = TaskType(tt)
            except ValueError:
                tt = TaskType.GENERAL
        src = data.get("source", SFTSource.MANUAL.value)
        if isinstance(src, str):
            try:
                src = SFTSource(src)
            except ValueError:
                src = SFTSource.MANUAL
        return cls(
            prompt=data["prompt"],
            completion=data["completion"],
            task_type=tt,
            quality_score=float(data.get("quality_score", 0.5)),
            source=src,
            timestamp=float(data.get("timestamp", time.time())),
            user_id=data.get("user_id", ""),
            redacted=bool(data.get("redacted", False)),
            prompt_hash=data.get("prompt_hash", ""),
        )

    def to_alpaca(self) -> dict[str, Any]:
        """Alpaca 格式:{"instruction", "input", "output"}。

        prompt 拆为 instruction + input(以 '\\n' 分隔,若无分隔则全部为 instruction)。
        """
        if "\n" in self.prompt:
            instruction, _, input_text = self.prompt.partition("\n")
        else:
            instruction, input_text = self.prompt, ""
        return {
            "instruction": instruction,
            "input": input_text,
            "output": self.completion,
        }

    def to_sharegpt(self) -> dict[str, Any]:
        """ShareGPT 格式:{"conversations": [{"from", "value"}]}。"""
        return {
            "conversations": [
                {"from": "human", "value": self.prompt},
                {"from": "gpt", "value": self.completion},
            ]
        }


# =====================================================================
# SFTDataset
# =====================================================================
class SFTDataset:
    """SFT 数据集构建器。

    用法:
        ds = SFTDataset()
        ds.add(SFTExample(prompt="如何立遗嘱?", completion="...", task_type=TaskType.LEGAL))
        ds.filter_by_quality(0.7)
        ds.deduplicate()
        ds.balance_classes()
        data = ds.export(ExportFormat.JSONL)
    """

    def __init__(
        self,
        pii_redactor: PIIRedactor | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._examples: list[SFTExample] = []
        self._pii_redactor = pii_redactor or get_pii_redactor()

    # ------------------------------------------------------------------
    # 添加
    # ------------------------------------------------------------------
    def add(self, example: SFTExample) -> bool:
        """添加一条 SFT 样本。

        流程:
            1. PII 脱敏(prompt + completion)
            2. 重算 prompt_hash(脱敏后可能变化)
            3. 质量分校验(< 0 抛 ValueError,0-1 之外 clamp)

        Returns:
            True 成功
        """
        with self._lock:
            # 1. PII 脱敏(强制,无论 redacted 字段)
            if not example.redacted:
                example.prompt = self._pii_redactor.redact(example.prompt).redacted_text
                example.completion = self._pii_redactor.redact(
                    example.completion
                ).redacted_text
                example.redacted = True

            # 2. 重算 hash(脱敏后)
            example.prompt_hash = _hash_text(example.prompt)

            # 3. 质量分校验
            if example.quality_score < 0:
                raise ValueError(f"quality_score must be >= 0, got {example.quality_score}")
            example.quality_score = max(0.0, min(1.0, example.quality_score))

            self._examples.append(example)
            return True

    def add_many(self, examples: Iterable[SFTExample]) -> int:
        """批量添加,返回成功条数。"""
        count = 0
        for ex in examples:
            if self.add(ex):
                count += 1
        return count

    def examples(self) -> list[SFTExample]:
        """返回当前样本快照(拷贝)。"""
        with self._lock:
            return list(self._examples)

    def count(self) -> int:
        with self._lock:
            return len(self._examples)

    def clear(self) -> None:
        with self._lock:
            self._examples.clear()

    # ------------------------------------------------------------------
    # 过滤(返回新 dataset,不改自身)
    # ------------------------------------------------------------------
    def filter_by_quality(self, min_score: float) -> SFTDataset:
        """按质量分过滤(返回新 dataset,保留 ≥ min_score 的样本)。"""
        new_ds = SFTDataset(pii_redactor=self._pii_redactor)
        with self._lock:
            for ex in self._examples:
                if ex.quality_score >= min_score:
                    # 已脱敏,直接 append,不走 add(避免重复脱敏)
                    new_ds._examples.append(ex)
        return new_ds

    def filter_by_task_type(self, task_type: TaskType) -> SFTDataset:
        """按任务类型过滤。"""
        new_ds = SFTDataset(pii_redactor=self._pii_redactor)
        with self._lock:
            for ex in self._examples:
                if ex.task_type == task_type:
                    new_ds._examples.append(ex)
        return new_ds

    # ------------------------------------------------------------------
    # 去重(就地修改)
    # ------------------------------------------------------------------
    def deduplicate(self) -> int:
        """按 prompt_hash 去重,返回移除条数。"""
        with self._lock:
            seen: set[str] = set()
            unique: list[SFTExample] = []
            for ex in self._examples:
                if ex.prompt_hash in seen:
                    continue
                seen.add(ex.prompt_hash)
                unique.append(ex)
            removed = len(self._examples) - len(unique)
            self._examples = unique
            return removed

    # ------------------------------------------------------------------
    # 类别均衡
    # ------------------------------------------------------------------
    def balance_classes(self) -> int:
        """按 task_type 均衡(下采样到最小类样本数)。

        Returns:
            移除条数
        """
        with self._lock:
            # 按 task_type 分组
            by_type: dict[TaskType, list[SFTExample]] = {}
            for ex in self._examples:
                by_type.setdefault(ex.task_type, []).append(ex)

            if not by_type:
                return 0

            # 找最小类样本数
            min_count = min(len(v) for v in by_type.values())

            # 各类下采样到 min_count(保留质量分高的)
            balanced: list[SFTExample] = []
            for _task_type, examples in by_type.items():
                # 按质量分降序排,取前 min_count
                sorted_ex = sorted(
                    examples, key=lambda e: e.quality_score, reverse=True
                )
                balanced.extend(sorted_ex[:min_count])

            removed = len(self._examples) - len(balanced)
            self._examples = balanced
            return removed

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def export(self, format: ExportFormat | str) -> bytes:
        """导出为指定格式,返回 bytes。

        支持:jsonl / csv / alpaca / sharegpt
        """
        if isinstance(format, str):
            try:
                format = ExportFormat(format)
            except ValueError as e:
                raise ValueError(f"Unknown export format: {format}") from e

        with self._lock:
            examples = list(self._examples)

        if format == ExportFormat.JSONL:
            return self._export_jsonl(examples)
        if format == ExportFormat.CSV:
            return self._export_csv(examples)
        if format == ExportFormat.ALPACA:
            return self._export_alpaca(examples)
        if format == ExportFormat.SHAREGPT:
            return self._export_sharegpt(examples)
        raise ValueError(f"Unsupported format: {format}")

    def _export_jsonl(self, examples: list[SFTExample]) -> bytes:
        buf = io.StringIO()
        for ex in examples:
            buf.write(json.dumps(ex.to_dict(), ensure_ascii=False) + "\n")
        return buf.getvalue().encode("utf-8")

    def _export_csv(self, examples: list[SFTExample]) -> bytes:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "prompt", "completion", "task_type", "quality_score",
            "source", "timestamp", "user_id", "redacted", "prompt_hash",
        ])
        for ex in examples:
            writer.writerow([
                ex.prompt, ex.completion, ex.task_type.value, ex.quality_score,
                ex.source.value, ex.timestamp, ex.user_id, ex.redacted, ex.prompt_hash,
            ])
        return buf.getvalue().encode("utf-8")

    def _export_alpaca(self, examples: list[SFTExample]) -> bytes:
        items = [ex.to_alpaca() for ex in examples]
        return json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8")

    def _export_sharegpt(self, examples: list[SFTExample]) -> bytes:
        items = [ex.to_sharegpt() for ex in examples]
        return json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8")

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def validate(self) -> dict[str, Any]:
        """校验数据集格式正确性。

        Returns:
            {"valid": bool, "errors": list[str], "warnings": list[str],
             "stats": {...}}
        """
        errors: list[str] = []
        warnings: list[str] = []
        with self._lock:
            seen_hashes: set[str] = set()
            task_dist: dict[str, int] = {}
            quality_sum = 0.0

            for i, ex in enumerate(self._examples):
                # 必填字段
                if not ex.prompt:
                    errors.append(f"example[{i}]: empty prompt")
                if not ex.completion:
                    errors.append(f"example[{i}]: empty completion")
                # 质量分范围
                if not (0 <= ex.quality_score <= 1):
                    errors.append(
                        f"example[{i}]: quality_score {ex.quality_score} out of [0,1]"
                    )
                # 必须脱敏
                if not ex.redacted:
                    errors.append(f"example[{i}]: not PII-redacted")
                # prompt_hash 一致性
                if ex.prompt_hash != _hash_text(ex.prompt):
                    errors.append(f"example[{i}]: prompt_hash mismatch")
                # 重复检测
                if ex.prompt_hash in seen_hashes:
                    warnings.append(f"example[{i}]: duplicate prompt_hash")
                seen_hashes.add(ex.prompt_hash)
                # 任务类型分布
                task_dist[ex.task_type.value] = task_dist.get(ex.task_type.value, 0) + 1
                quality_sum += ex.quality_score

            stats = {
                "total": len(self._examples),
                "task_distribution": task_dist,
                "avg_quality": (quality_sum / len(self._examples)) if self._examples else 0.0,
            }

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "stats": stats,
        }

    # ------------------------------------------------------------------
    # 持久化(到 tenant 数据目录)
    # ------------------------------------------------------------------
    def save(self, filename: str = "alignment/sft_dataset.jsonl") -> Path:
        """原子写入 tenant 数据目录(JSONL)。"""
        target = resolve_data_path(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        with self._lock:
            examples = list(self._examples)
        with open(tmp, "w", encoding="utf-8") as f:
            for ex in examples:
                f.write(json.dumps(ex.to_dict(), ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        return target

    def load(self, filename: str = "alignment/sft_dataset.jsonl") -> int:
        """从 tenant 数据目录加载 JSONL。返回加载条数。"""
        target = resolve_data_path(filename)
        if not target.exists():
            return 0
        loaded = 0
        with self._lock:
            self._examples.clear()
            with open(target, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        ex = SFTExample.from_dict(data)
                        # 加载的样本已脱敏,直接 append
                        self._examples.append(ex)
                        loaded += 1
                    except (json.JSONDecodeError, KeyError, ValueError) as e:
                        logger.warning("Skip malformed SFT line: %s", e)
        return loaded

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        """返回数据集统计。"""
        with self._lock:
            task_dist: dict[str, int] = {}
            source_dist: dict[str, int] = {}
            quality_sum = 0.0
            for ex in self._examples:
                task_dist[ex.task_type.value] = task_dist.get(ex.task_type.value, 0) + 1
                source_dist[ex.source.value] = source_dist.get(ex.source.value, 0) + 1
                quality_sum += ex.quality_score
            return {
                "total": len(self._examples),
                "task_distribution": task_dist,
                "source_distribution": source_dist,
                "avg_quality": (quality_sum / len(self._examples)) if self._examples else 0.0,
                "all_redacted": all(ex.redacted for ex in self._examples),
            }


# =====================================================================
# 内部工具
# =====================================================================
def _hash_text(text: str) -> str:
    """对文本做 SHA-256,返回前 16 位(用于去重)。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
