"""免责告知模块 - 遵守 compliance / service-boundary / transparency / legal-compliance 规则

提供 4 类告知文本：
1. 平台身份告知（开场）
2. 法律意见免责（涉及法律问题时）
3. 代办边界免责（用户要求代办时）
4. 数据准确性免责（涉及具体电话/费用/时限时）
"""

from .text import DisclaimerBuilder

__all__ = ["DisclaimerBuilder"]
