"""P8.1.3 用量计量 - 4 维度计量 + 事件流持久化。

计量维度:
    - llm_tokens: LLM 调用 token(prompt + completion)
    - tool_calls: MCP 工具调用次数
    - storage: 存储使用量(MB)
    - multimodal_calls: 多模态调用(OCR / ASR / TTS / Vision / ImageGen)

设计:
    - 事件流持久化(append-only JSONL,与 P7.6 durable_execution 同款)
    - 计量按 user_id + tenant_id + period 索引
    - 与 P7.7 quota 协同:quota 是"限制执行",metering 是"原始事件"
    - 支持实时聚合(看板用)+ 离线聚合(账单用)

feature flag:`DEADMAN_BILLING_ENABLED=0` 关闭时不记录(避免 IO 开销)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import get_current_tenant_id

logger = logging.getLogger(__name__)


class MeteringDimension(str, Enum):
    """计量维度。"""

    LLM_TOKENS = "llm_tokens"
    TOOL_CALLS = "tool_calls"
    STORAGE = "storage_mb"
    MULTIMODAL = "multimodal_calls"


@dataclass
class MeteringEvent:
    """单条计量事件(append-only)。"""

    timestamp: float
    user_id: str
    tenant_id: str
    dimension: str  # MeteringDimension.value
    amount: int
    # 可选元数据(便于审计)
    model: str = ""  # 用了哪个 LLM(仅 LLM_TOKENS)
    tool_name: str = ""  # 调了哪个工具(仅 TOOL_CALLS)
    multimodal_type: str = ""  # OCR/ASR/TTS/Vision/ImageGen(仅 MULTIMODAL)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> MeteringEvent:
        return cls(
            timestamp=data["timestamp"],
            user_id=data["user_id"],
            tenant_id=data["tenant_id"],
            dimension=data["dimension"],
            amount=data["amount"],
            model=data.get("model", ""),
            tool_name=data.get("tool_name", ""),
            multimodal_type=data.get("multimodal_type", ""),
        )


class MeteringService:
    """用量计量服务 - 写入事件流 + 查询聚合。

    设计:
        - 事件流:`data/billing/metering_<YYYYMMDD>.jsonl`(按天分文件,便于归档)
        - 内存聚合缓存(避免每次全表扫描)
        - 异步写盘(可选,默认同步)
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or Path(
            os.environ.get("DEADMAN_METERING_DIR", "data/billing/metering")
        )
        self._lock = threading.RLock()
        # 内存聚合缓存:{(user_id, dimension, period): total}
        # period 格式:"YYYY-MM"(月) / "YYYY-MM-DD"(日)
        self._aggregate_cache: dict[tuple[str, str, str], int] = {}

    def record(
        self,
        user_id: str,
        dimension: MeteringDimension,
        amount: int,
        tenant_id: str | None = None,
        model: str = "",
        tool_name: str = "",
        multimodal_type: str = "",
    ) -> MeteringEvent | None:
        """记录一条计量事件。

        Args:
            amount: 数量(tokens / calls / MB)
            model: LLM 模型名(仅 LLM_TOKENS)
            tool_name: 工具名(仅 TOOL_CALLS)
            multimodal_type: 多模态类型(仅 MULTIMODAL)

        Returns:
            MeteringEvent(若 billing 关闭返回 None)
        """
        if not is_enabled("billing"):
            return None

        if amount < 0:
            logger.warning("Negative metering amount: %d (dimension=%s, user=%s)", amount, dimension.value, user_id)
            return None

        tid = tenant_id or get_current_tenant_id()
        event = MeteringEvent(
            timestamp=time.time(),
            user_id=user_id,
            tenant_id=tid,
            dimension=dimension.value,
            amount=amount,
            model=model,
            tool_name=tool_name,
            multimodal_type=multimodal_type,
        )

        try:
            self._write_event(event)
            # 更新内存聚合缓存
            self._update_cache(event)
        except Exception as e:
            logger.error("Metering write failed: %s", e)
        return event

    # ==================================================================
    # 便捷方法
    # ==================================================================

    def record_llm_tokens(
        self,
        user_id: str,
        tokens: int,
        model: str = "",
        tenant_id: str | None = None,
    ) -> MeteringEvent | None:
        return self.record(
            user_id,
            MeteringDimension.LLM_TOKENS,
            tokens,
            tenant_id=tenant_id,
            model=model,
        )

    def record_tool_call(
        self,
        user_id: str,
        tool_name: str = "",
        tenant_id: str | None = None,
    ) -> MeteringEvent | None:
        return self.record(
            user_id,
            MeteringDimension.TOOL_CALLS,
            1,
            tenant_id=tenant_id,
            tool_name=tool_name,
        )

    def record_storage(
        self,
        user_id: str,
        bytes_: int,
        tenant_id: str | None = None,
    ) -> MeteringEvent | None:
        # bytes → MB(向上取整,避免小数)
        mb = (bytes_ + 1024 * 1024 - 1) // (1024 * 1024)
        return self.record(
            user_id,
            MeteringDimension.STORAGE,
            mb,
            tenant_id=tenant_id,
        )

    def record_multimodal(
        self,
        user_id: str,
        multimodal_type: str,
        tenant_id: str | None = None,
    ) -> MeteringEvent | None:
        return self.record(
            user_id,
            MeteringDimension.MULTIMODAL,
            1,
            tenant_id=tenant_id,
            multimodal_type=multimodal_type,
        )

    # ==================================================================
    # 查询聚合
    # ==================================================================

    def aggregate(
        self,
        user_id: str,
        dimension: MeteringDimension,
        period: str,
    ) -> int:
        """查某用户某维度某周期总用量。

        Args:
            period: "YYYY-MM" 月 / "YYYY-MM-DD" 日 / "all" 全部
        """
        if not is_enabled("billing"):
            return 0

        # 先查缓存
        cache_key = (user_id, dimension.value, period)
        with self._lock:
            if cache_key in self._aggregate_cache:
                return self._aggregate_cache[cache_key]

        # 缓存 miss → 全表扫描
        total = self._aggregate_from_disk(user_id, dimension, period)
        with self._lock:
            self._aggregate_cache[cache_key] = total
        return total

    def get_daily_usage(self, user_id: str, date: str) -> dict[str, int]:
        """查某日各维度用量。

        Args:
            date: "YYYY-MM-DD"
        """
        result = {d.value: 0 for d in MeteringDimension}
        if not is_enabled("billing"):
            return result

        # 读单日文件
        file_path = self._file_for_date(date)
        if not file_path.exists():
            return result

        try:
            for line in file_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    event = MeteringEvent.from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError):
                    continue
                if event.user_id != user_id:
                    continue
                # 日期匹配(用 timestamp 转 date 比对,容错时区)
                event_date = time.strftime("%Y-%m-%d", time.localtime(event.timestamp))
                if event_date == date:
                    result[event.dimension] = result.get(event.dimension, 0) + event.amount
        except Exception as e:
            logger.warning("Metering daily aggregate failed: %s", e)

        return result

    def get_monthly_usage(self, user_id: str, year_month: str) -> dict[str, int]:
        """查某月各维度用量。

        Args:
            year_month: "YYYY-MM"
        """
        result = {d.value: 0 for d in MeteringDimension}
        if not is_enabled("billing"):
            return result

        # 遍历该月所有日文件
        year, month = year_month.split("-")
        for day in range(1, 32):
            date = f"{year}-{month}-{day:02d}"
            daily = self.get_daily_usage(user_id, date)
            for k, v in daily.items():
                result[k] = result.get(k, 0) + v
        return result

    # ==================================================================
    # 内部
    # ==================================================================

    def _file_for_date(self, date_str: str) -> Path:
        """事件文件路径(按天)。"""
        return self.data_dir / f"metering_{date_str}.jsonl"

    def _file_for_event(self, event: MeteringEvent) -> Path:
        date_str = time.strftime("%Y-%m-%d", time.localtime(event.timestamp))
        return self._file_for_date(date_str)

    def _write_event(self, event: MeteringEvent) -> None:
        file_path = self._file_for_event(event)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        # append-only(单次 append 是 POSIX 原子的,长度 < PIPE_BUF)
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def _update_cache(self, event: MeteringEvent) -> None:
        """事件写入后更新内存缓存。"""
        event_date = time.strftime("%Y-%m-%d", time.localtime(event.timestamp))
        event_month = event_date[:7]
        for period in (event_date, event_month, "all"):
            key = (event.user_id, event.dimension, period)
            with self._lock:
                self._aggregate_cache[key] = self._aggregate_cache.get(key, 0) + event.amount

    def _aggregate_from_disk(
        self,
        user_id: str,
        dimension: MeteringDimension,
        period: str,
    ) -> int:
        """从磁盘扫描聚合(缓存 miss 时)。"""
        total = 0
        if period == "all":
            files = sorted(self.data_dir.glob("metering_*.jsonl"))
        elif len(period) == 7:  # YYYY-MM
            files = sorted(self.data_dir.glob(f"metering_{period}-*.jsonl"))
        else:  # YYYY-MM-DD
            files = [self._file_for_date(period)]
            if not files[0].exists():
                return 0

        for fpath in files:
            if not fpath.exists():
                continue
            try:
                for line in fpath.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        event = MeteringEvent.from_dict(json.loads(line))
                    except (json.JSONDecodeError, KeyError):
                        continue
                    if event.user_id == user_id and event.dimension == dimension.value:
                        total += event.amount
            except Exception as e:
                logger.warning("Metering scan file %s failed: %s", fpath, e)
        return total


# 全局单例
_ms_instance: MeteringService | None = None
_ms_lock = threading.Lock()


def get_metering_service() -> MeteringService:
    global _ms_instance
    if _ms_instance is None:
        with _ms_lock:
            if _ms_instance is None:
                _ms_instance = MeteringService()
    return _ms_instance
