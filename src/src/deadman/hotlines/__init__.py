"""官方热线查询模块 - 仅提供公开官方热线，不编造号码

遵守 compliance-framework.md：
- 不编造电话号码、地址、办公时间
- 引导用户通过官方渠道核实

所有热线必须标 source 字段（数据来源）。
"""

from .lookup import HotlineLookup

__all__ = ["HotlineLookup"]
