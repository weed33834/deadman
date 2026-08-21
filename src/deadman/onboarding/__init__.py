"""新用户引导向导 - Phase 16C

提供 5 步引导，收集用户画像：
1. relationship：与逝者的关系
2. location：所在地点
3. death_date：逝者去世日期
4. current_stage：当前办理进度
5. consent：免责声明同意

完成后将画像持久化，后续可注入 ConversationState.user_profile。

模块组成：
- models.py：OnboardingProfile 数据类
- wizard.py：OnboardingWizard 引导逻辑
- store.py：OnboardingStore 原子写入
"""

from __future__ import annotations

from .models import OnboardingProfile
from .store import OnboardingStore
from .wizard import OnboardingWizard

__all__ = [
    "OnboardingProfile",
    "OnboardingStore",
    "OnboardingWizard",
]
