"""哀伤陪伴模块"""

from .companion import (
    COMPANION_SYSTEM_PROMPT,
    CRISIS_HOTLINE_TEXT,
    CrisisAssessment,
    companion_reply,
    detect_crisis,
)

__all__ = [
    "CRISIS_HOTLINE_TEXT",
    "COMPANION_SYSTEM_PROMPT",
    "CrisisAssessment",
    "companion_reply",
    "detect_crisis",
]
