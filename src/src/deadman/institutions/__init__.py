"""殡葬机构查询模块 - 数据来自公开政务平台

遵守 retrieval-guardrails.md：
- 高可信：2 个以上官方源确认且 1 年内 → 可直接陈述
- 中可信：1 个官方源或多个非官方源一致 → 加注"建议办理前核实"
- 低可信：单一非官方源或推断 → 加注"仅作参考，请向 [机构] 核实"
- 不可信：无来源/过期/用户报告错误 → 不得用于给用户的具体引导
"""

from .store import Institution, InstitutionStore

__all__ = ["Institution", "InstitutionStore"]
