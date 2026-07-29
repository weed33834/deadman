"""memorial_writer - AI 悼文/讣告/答谢词/墓志铭/追思会致辞生成

参考竞品 Toast + Empathy（Toast 已用此功能服务 70000+ 家庭）。

5 种 doc_type：
    - eulogy          悼文（500-800 字）
    - obituary        讣告（200-400 字，含生卒日期/丧礼时间地点）
    - thank_you_note  答谢词（200-400 字，家属对吊唁者的感谢）
    - epitaph         墓志铭（20-100 字，简短铭文）
    - memorial_speech 追思会致辞（500-1000 字）

多语言：zh-CN（现代文）/ zh-Classical（古文）/ en-US
多信仰：none / buddhist / taoist / christian
多语气：solemn（庄重）/ warm（温暖）/ humorous（幽默但得体）

合规关联：
    - PIPL 第五章：PII 脱敏（不要求用户提供真实姓名，decedent_name 不存盘）
    - integrity-framework.md：不编造未提供的特质/回忆
    - safety-protocol.md：输出含自伤/暴力内容时打 safety_flags
    - service-boundary-framework.md：附"AI 生成仅供参考"边界告知
"""

from __future__ import annotations

from .generator import MemorialGenerator
from .models import MemorialRequest, MemorialResult

__all__ = [
    "MemorialGenerator",
    "MemorialRequest",
    "MemorialResult",
]
