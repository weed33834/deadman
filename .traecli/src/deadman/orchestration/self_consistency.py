"""Self-Consistency - 同一问题多次采样 + 投票

P1.4 实现（v1.2 计划文档 P1.4）：对同一问题用较高温度采样 N 次（默认 3，
temperature=0.7），按多数投票（简单多数 / 加权 by confidence）选出最终答案。
参考 Wang et al., 2022。

核心方法：
- sample(query, n=3, temperature=0.7) -> list[str]: 同一问题采样 N 次
- aggregate(answers, confidences=None) -> str: 投票（多数 / 加权）
- solve(query, n, temperature) -> ConsistencyResult: 完整求解

设计要点：
- 简单多数：归一化（strip + lower）后 Counter 投票
- 加权投票：每个答案按 confidence 加权累计，取最高
- 平票 → 取第一个出现的高票答案
- 采样温度随序号微调（+0.05 * i）增多样性

韧性 / 安全特性（三大铁律）：
- feature flag: DEADMAN_SELF_CONSISTENCY_ENABLED=0（默认关闭）
- LLM 不可用 → sample 返回 []
- 单次采样失败 → 跳过该次，不影响其他
- 全部采样失败 → aggregate 返回 ""
- answers 为空 → aggregate 返回 ""

降级路径：
- LLM 不可用 → ConsistencyResult(degraded=True)
- 无采样结果 → ConsistencyResult(degraded=True)
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..llm import LLMClient

logger = logging.getLogger(__name__)

# =====================================================================
# 配置（全部 feature flag，默认安全）
# =====================================================================

# Self-Consistency 总开关：默认关闭
SELF_CONSISTENCY_ENABLED: bool = os.environ.get(
    "DEADMAN_SELF_CONSISTENCY_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")

# 默认采样数
SELF_CONSISTENCY_DEFAULT_N: int = int(
    os.environ.get("DEADMAN_SELF_CONSISTENCY_N", "3")
)

# 默认采样温度
SELF_CONSISTENCY_DEFAULT_TEMP: float = float(
    os.environ.get("DEADMAN_SELF_CONSISTENCY_TEMP", "0.7")
)


# =====================================================================
# 数据结构
# =====================================================================


@dataclass
class ConsistencyResult:
    """Self-Consistency 投票结果"""

    final_answer: str = ""
    votes: dict[str, int] = field(default_factory=dict)
    samples: list[str] = field(default_factory=list)
    degraded: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_answer": self.final_answer,
            "votes": dict(self.votes),
            "samples": list(self.samples),
            "degraded": self.degraded,
            "note": self.note,
        }


# =====================================================================
# Prompt 模板
# =====================================================================


_SAMPLE_PROMPT = """请回答以下问题。

问题: {query}

回答（直接给答案，不要解释）:"""


# =====================================================================
# SelfConsistency
# =====================================================================


class SelfConsistency:
    """同一问题多次采样 + 投票

    用法：
        sc = SelfConsistency(llm=strong_llm)
        samples = await sc.sample(query, n=3)
        answer = sc.aggregate(samples)
        # 或一步到位：
        result = await sc.solve(query)
    """

    def __init__(
        self,
        llm: LLMClient | None = None,
        default_n: int = SELF_CONSISTENCY_DEFAULT_N,
        default_temperature: float = SELF_CONSISTENCY_DEFAULT_TEMP,
    ) -> None:
        self.llm = llm
        self.default_n = max(1, default_n)
        self.default_temperature = default_temperature

    async def sample(
        self,
        query: str,
        n: int = 3,
        temperature: float = 0.7,
    ) -> list[str]:
        """同一问题采样 N 次（温度调高增多样性）

        Args:
            query: 用户问题
            n: 采样数
            temperature: 基础温度（实际随序号微调 +0.05*i）

        Returns:
            采样结果列表（LLM 不可用返回 []；单次失败跳过）
        """
        if not self.llm or not getattr(self.llm, "api_key", ""):
            return []
        n = max(1, n)
        prompt = _SAMPLE_PROMPT.format(query=query)
        messages = [{"role": "user", "content": prompt}]
        samples: list[str] = []
        for i in range(n):
            # 温度随采样序号微调（增多样性）
            temp = max(0.0, temperature + i * 0.05)
            try:
                answer = await self.llm.chat(messages, temperature=temp)
                if answer:
                    samples.append(answer)
            except Exception as e:
                logger.warning("SelfConsistency 采样 %d 失败: %s", i, e)
        return samples

    def aggregate(
        self,
        answers: list[str],
        confidences: list[float] | None = None,
    ) -> str:
        """投票聚合：简单多数 / 加权 by confidence

        Args:
            answers: 采样答案列表
            confidences: 可选的置信度列表（与 answers 等长）；None 表示简单多数

        Returns:
            最终答案；answers 为空返回 ""；平票取第一个出现的高票答案
        """
        if not answers:
            return ""
        # 归一化（strip + lower）后投票，但返回原始大小写
        normalized = [a.strip().lower() for a in answers]

        if confidences is None or len(confidences) != len(answers):
            # 简单多数投票
            counter = Counter(normalized)
            best_norm = counter.most_common(1)[0][0]
            # 找原始答案（保持大小写）
            for orig, norm in zip(answers, normalized):
                if norm == best_norm:
                    return orig
            return answers[0]

        # 加权投票
        weighted: dict[str, float] = {}
        for norm, conf in zip(normalized, confidences):
            weighted[norm] = weighted.get(norm, 0.0) + float(conf)
        best_norm = max(weighted, key=weighted.get)
        for orig, norm in zip(answers, normalized):
            if norm == best_norm:
                return orig
        return answers[0]

    async def solve(
        self,
        query: str,
        n: int | None = None,
        temperature: float | None = None,
    ) -> ConsistencyResult:
        """完整 Self-Consistency 求解：采样 + 投票

        Args:
            query: 用户问题
            n: 采样数（None 用 default_n）
            temperature: 采样温度（None 用 default_temperature）

        Returns:
            ConsistencyResult，degraded=True 表示 LLM 不可用或无采样结果
        """
        result = ConsistencyResult()
        if not self.llm or not getattr(self.llm, "api_key", ""):
            result.degraded = True
            result.note = "LLM 未配置，Self-Consistency 降级"
            return result

        n = n or self.default_n
        temp = temperature if temperature is not None else self.default_temperature
        samples = await self.sample(query, n=n, temperature=temp)
        if not samples:
            result.degraded = True
            result.note = "无采样结果"
            return result

        result.samples = samples
        final = self.aggregate(samples)
        result.final_answer = final
        # 投票统计（归一化后）
        normalized = [s.strip().lower() for s in samples]
        result.votes = dict(Counter(normalized))
        return result
