"""思维意识识别层

理解用户输入的意图与安全状态，为上层编排提供路由依据。
"""

from .intent import IntentResult, IntentType, classify_intent, classify_intent_keyword
from .recognizer import AwarenessResult, assess

__all__ = [
    "IntentType",
    "IntentResult",
    "classify_intent",
    "classify_intent_keyword",
    "AwarenessResult",
    "assess",
]
