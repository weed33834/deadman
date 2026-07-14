"""成本与配额追踪 - token 用量 → 成本计算 + 预警

设计目标(对应用户需求"成本与配额追踪"):
  - 每次 LLM 调用记录 token 用量,按 PROVIDER_MODELS 价格算成本
  - 持久化到 data/llm_cost.json(供看板/结算消费)
  - 记录到 metrics_collector(efficiency.cost_per_dialogue_usd 等)
  - 配额预警:单 provider 成本超阈值时告警

用法:
    from legacy.cost import cost_tracker
    cost_tracker.record_usage("openai", "gpt-5.5", prompt=100, completion=50)
    summary = cost_tracker.get_summary()
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import settings
from .observability import metrics_collector

logger = logging.getLogger(__name__)


@dataclass
class UsageRecord:
    """单次调用用量记录"""

    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    timestamp: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _lookup_price(provider: str, model: str) -> tuple[float, float] | None:
    """查 PROVIDER_MODELS 价格,返回 (input_per_1M, output_per_1M) USD

    价格单位:美元 / 1M tokens(PROVIDER_MODELS 里的字段就是这个单位)
    """
    # 延迟导入避免循环依赖
    from .llm import PROVIDER_MODELS

    models = PROVIDER_MODELS.get(provider, [])
    for m in models:
        if m.get("id") == model:
            inp = m.get("input_price")
            out = m.get("output_price")
            if inp is None or out is None:
                return None
            return float(inp), float(out)
    return None


def calc_cost(
    provider: str, model: str, prompt_tokens: int, completion_tokens: int
) -> float:
    """计算单次调用成本(USD)

    价格表查不到时返回 0.0 并记录 warning(本地模型/未配置价格的模型)。
    """
    price = _lookup_price(provider, model)
    if price is None:
        return 0.0
    input_price, output_price = price
    # 价格单位是 USD / 1M tokens
    cost = (prompt_tokens / 1_000_000) * input_price + (
        completion_tokens / 1_000_000
    ) * output_price
    return round(cost, 6)


class CostTracker:
    """成本追踪器 - 内存累计 + 持久化 + 配额预警

    持久化策略:每次 record_usage 后追加写 data/llm_cost.json(全量快照),
    便于看板直接读。同时记录到 metrics_collector 做实时聚合。
    """

    def __init__(self) -> None:
        self._records: list[UsageRecord] = []
        self._cost_file = settings.project_root / "data" / "llm_cost.json"
        # 单 provider 日成本预警阈值(USD),0 表示不预警
        self._alert_threshold_usd = float(os.getenv("LLM_COST_ALERT_USD", "10.0"))
        self._load()

    def _load(self) -> None:
        """启动时加载历史记录(便于跨进程累计)"""
        if not self._cost_file.exists():
            return
        try:
            with open(self._cost_file, encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("records", []):
                self._records.append(UsageRecord(**item))
        except (OSError, json.JSONDecodeError, TypeError) as e:
            logger.warning("加载成本记录失败: %s", e)

    def _persist(self) -> None:
        """持久化全量快照到 data/llm_cost.json"""
        self._cost_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now().isoformat(),
            "total_records": len(self._records),
            "records": [
                {
                    "provider": r.provider,
                    "model": r.model,
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "cost_usd": r.cost_usd,
                    "timestamp": r.timestamp,
                    "metadata": r.metadata,
                }
                for r in self._records
            ],
        }
        try:
            with open(self._cost_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning("持久化成本记录失败: %s", e)

    def record_usage(
        self,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        metadata: dict[str, Any] | None = None,
    ) -> UsageRecord:
        """记录一次 LLM 调用的 token 用量并计算成本

        Returns:
            UsageRecord(含 cost_usd)
        """
        cost = calc_cost(provider, model, prompt_tokens, completion_tokens)
        record = UsageRecord(
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            timestamp=datetime.now().isoformat(),
            metadata=metadata or {},
        )
        self._records.append(record)
        self._persist()

        # 记录到 metrics_collector
        tags = {"provider": provider, "model": model}
        metrics_collector.record_metric(
            "efficiency.token_input_count", prompt_tokens, tags=tags
        )
        metrics_collector.record_metric(
            "efficiency.token_output_count", completion_tokens, tags=tags
        )
        metrics_collector.record_metric(
            "efficiency.cost_per_dialogue_usd", cost, tags=tags
        )

        # 配额预警
        if self._alert_threshold_usd > 0:
            summary = self.get_summary()
            provider_cost = summary["by_provider"].get(provider, {}).get("cost_usd", 0.0)
            if provider_cost >= self._alert_threshold_usd:
                logger.warning(
                    "⚠️ 配额预警: provider=%s 累计成本 $%.4f 已超阈值 $%.2f",
                    provider,
                    provider_cost,
                    self._alert_threshold_usd,
                )
        return record

    def get_summary(self) -> dict[str, Any]:
        """汇总:总成本/总token + 按 provider/model 分组"""
        total_cost = 0.0
        total_prompt = 0
        total_completion = 0
        by_provider: dict[str, dict[str, Any]] = {}
        by_model: dict[str, dict[str, Any]] = {}

        for r in self._records:
            total_cost += r.cost_usd
            total_prompt += r.prompt_tokens
            total_completion += r.completion_tokens

            p = by_provider.setdefault(
                r.provider, {"cost_usd": 0.0, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
            )
            p["cost_usd"] += r.cost_usd
            p["calls"] += 1
            p["prompt_tokens"] += r.prompt_tokens
            p["completion_tokens"] += r.completion_tokens

            m_key = f"{r.provider}/{r.model}"
            m = by_model.setdefault(
                m_key, {"cost_usd": 0.0, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
            )
            m["cost_usd"] += r.cost_usd
            m["calls"] += 1
            m["prompt_tokens"] += r.prompt_tokens
            m["completion_tokens"] += r.completion_tokens

        return {
            "total_cost_usd": round(total_cost, 6),
            "total_calls": len(self._records),
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "by_provider": by_provider,
            "by_model": by_model,
        }

    def clear(self) -> None:
        """清空记录(测试/重置用)"""
        self._records.clear()
        self._persist()


# 全局单例
cost_tracker = CostTracker()
