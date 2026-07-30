"""SelfCheckGPT 数字类幻觉检测 - 通过多次采样一致性检测数字类输出的幻觉"""

from __future__ import annotations

import re

from ..config import settings
from ..llm import LLMClient

# 数字类 claim 正则模式（6 种）
NUMBER_PATTERNS: dict[str, str] = {
    "phone": r"\b\d{3,4}[-\s]?\d{7,8}\b|\b\d{11}\b",  # 电话号码
    "days": r"\b\d+\s*(?:天|个工作日|日)\b",  # 时限
    "money": r"\b\d+(?:\.\d+)?\s*(?:万|元|美元|人民币)\b",  # 金额
    "percent": r"\b\d+(?:\.\d+)?\s*%",  # 百分比
    "article": r"第\s*\d+\s*条",  # 法条号
    "step_count": r"\b\d+\s*(?:步|个阶段|个环节)\b",  # 步骤数
}

# "高"一致性阈值（区分"高"与"中"）
_HIGH_THRESHOLD = 0.8


class SelfCheckChecker:
    """SelfCheckGPT 数字类幻觉检测器

    通过对同一 prompt 多次采样（不同 temperature），
    比较数字类 claim 的一致性，判断模型是否在"脑补"数字。

    一致性标签阈值：
      - >= 0.8：高
      - >= settings.selfcheck_consistency_threshold（默认 0.5）且 < 0.8：中
      - < settings.selfcheck_consistency_threshold（默认 0.5）：未知
    """

    def __init__(self) -> None:
        self.sample_count: int = settings.selfcheck_sample_count
        self.temperatures: list[float] = list(settings.selfcheck_temperatures)
        # 一致性阈值（区分"中"与"未知"），从 settings 获取
        self.consistency_threshold: float = settings.selfcheck_consistency_threshold

    async def extract_numeric_claims(self, text: str) -> list[dict]:
        """从文本中提取所有数字类 claim

        用 6 种正则模式扫描文本，返回所有匹配的数字类 claim。
        返回格式：[{"claim": "30天", "type": "days", "position": 123}, ...]
        """
        claims: list[dict] = []
        for claim_type, pattern in NUMBER_PATTERNS.items():
            for m in re.finditer(pattern, text):
                claims.append(
                    {
                        "claim": m.group(),
                        "type": claim_type,
                        "position": m.start(),
                    }
                )
        # 按出现位置排序，便于阅读和后续处理
        claims.sort(key=lambda c: c["position"])
        return claims

    async def check_consistency(
        self,
        original_response: str,
        sampled_responses: list[str],
    ) -> dict:
        """检查数字类 claim 在多次采样中的一致性

        对原始响应中的每个 numeric claim，统计其在采样响应中出现的次数。
        一致性 = 出现该 claim 的采样数 / 总采样数。

        返回格式：
            {"claims": [{"claim": "30天", "consistency": 0.9, "label": "高"}],
             "overall_consistency": 0.85}
        """
        # 提取原始响应中的所有数字类 claim
        original_claims = await self.extract_numeric_claims(original_response)

        total_samples = len(sampled_responses)
        if total_samples == 0:
            return {"claims": [], "overall_consistency": 0.0}

        # 从每次采样响应中提取数字类 claim，用于逐一比对
        sampled_claims_per_response: list[list[dict]] = []
        for resp in sampled_responses:
            sampled_claims_per_response.append(await self.extract_numeric_claims(resp))

        claim_results: list[dict] = []
        consistency_scores: list[float] = []

        for claim in original_claims:
            norm_claim = self._normalize(claim["claim"])
            match_count = 0
            for sampled_claims in sampled_claims_per_response:
                # 同类型且归一化后文本相同的 claim 视为匹配
                # 归一化可处理 "30 天" 与 "30天" 这类空白差异
                found = any(
                    sc["type"] == claim["type"] and self._normalize(sc["claim"]) == norm_claim
                    for sc in sampled_claims
                )
                if found:
                    match_count += 1

            consistency = match_count / total_samples
            label = self._label_for_consistency(consistency)

            claim_results.append(
                {
                    "claim": claim["claim"],
                    "type": claim["type"],
                    "position": claim["position"],
                    "consistency": consistency,
                    "label": label,
                }
            )
            consistency_scores.append(consistency)

        overall_consistency = (
            sum(consistency_scores) / len(consistency_scores) if consistency_scores else 0.0
        )

        return {
            "claims": claim_results,
            "overall_consistency": overall_consistency,
        }

    async def check(
        self,
        response: str,
        messages: list[dict[str, str]],
        llm_client: LLMClient,
    ) -> dict:
        """主入口 - 对响应做 SelfCheckGPT 数字类一致性校验

        流程：
          1. 提取 numeric claims
          2. 若无 numeric claims，直接通过
          3. 自适应多次采样（temperatures 从 settings 获取）
          4. 计算一致性
          5. 一致性 >= 0.8 标"高"，0.5-0.8 标"中"，< 0.5 标"未知"
          6. 汇总返回
        """
        # 1. 提取 numeric claims
        claims = await self.extract_numeric_claims(response)

        # 2. 若无 numeric claims，直接通过
        if not claims:
            return {"passed": True, "reason": "no_numeric_claims"}

        # 3. 自适应多次采样
        sampled_responses = await self._adaptive_sample(messages, llm_client, response)

        # 4. 计算一致性
        consistency_result = await self.check_consistency(response, sampled_responses)

        # 5. 整理每个 claim 的一致性分数与标签
        consistency_scores = [
            {
                "claim": c["claim"],
                "type": c["type"],
                "consistency": c["consistency"],
                "label": c["label"],
            }
            for c in consistency_result["claims"]
        ]
        overall_consistency = consistency_result["overall_consistency"]

        # 低一致性 claim（一致性 < 阈值，即标签为"未知"的）
        low_consistency_claims = [
            {
                "claim": c["claim"],
                "type": c["type"],
                "consistency": c["consistency"],
                "label": c["label"],
            }
            for c in consistency_result["claims"]
            if c["consistency"] < self.consistency_threshold
        ]

        # 6. 是否通过：不存在低一致性 claim 即视为通过
        passed = len(low_consistency_claims) == 0

        return {
            "passed": passed,
            "numeric_claims_found": len(claims),
            "consistency_scores": consistency_scores,
            "overall_consistency": overall_consistency,
            "low_consistency_claims": low_consistency_claims,
        }

    async def _adaptive_sample(
        self,
        messages: list[dict[str, str]],
        llm_client: LLMClient,
        original_response: str,
    ) -> list[str]:
        """自适应采样

        - 第一轮采样 3 次
        - 若一致性 >= 0.8，不再继续采样
        - 若一致性 < 阈值（默认 0.5），追加采样到 self.sample_count 次（默认 5）
        - 一致性中等则保持 3 次
        """
        # 第一轮：取前 3 个 temperature
        first_round_temps = self.temperatures[:3]
        sampled = await llm_client.sample_multiple(messages, first_round_temps)

        # 计算第一轮一致性，决定是否追加采样
        consistency_result = await self.check_consistency(original_response, sampled)
        overall = consistency_result["overall_consistency"]

        if overall >= _HIGH_THRESHOLD:
            # 一致性高，无需继续采样
            return sampled

        if overall < self.consistency_threshold:
            # 一致性低，追加采样到 sample_count 次
            remaining_temps = self.temperatures[3 : self.sample_count]
            if remaining_temps:
                additional = await llm_client.sample_multiple(messages, remaining_temps)
                sampled.extend(additional)
            return sampled

        # 一致性中等，保持当前 3 次采样
        return sampled

    @staticmethod
    def _normalize(text: str) -> str:
        """归一化文本：去除所有空白字符，便于比对

        例如 "30 天" -> "30天"，"第 1145 条" -> "第1145条"
        """
        return re.sub(r"\s+", "", text)

    def _label_for_consistency(self, consistency: float) -> str:
        """根据一致性分数返回标签

        - >= 0.8：高
        - >= 阈值（默认 0.5）且 < 0.8：中
        - < 阈值（默认 0.5）：未知
        """
        if consistency >= _HIGH_THRESHOLD:
            return "高"
        if consistency >= self.consistency_threshold:
            return "中"
        return "未知"
