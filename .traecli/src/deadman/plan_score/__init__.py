"""plan_score - 身后事规划完整度评分（Phase 15）

竞品对标：Trust & Will EstateOS 的 Plan Strength Score
    （百分比评分 + 缺失项清单 + 智能建议）

deadman 差异化：
    - 综合评估 ending_note（终活笔记）/ vault（数字遗产保险库）/
      decedent_id（遗码通案例）/ deadman_switch（失联开关）/
      basic_info（用户基础信息）五个维度的完整度
    - 输出统一 0-100 评分 + 缺失项清单 + top 3 优先建议

合规关联：
    - integrity-framework.md L1：评分基于实际数据（实际加载到的笔记/案例/条目），
      不编造数据，不猜测未填字段的内容
    - service-boundary-framework.md L3：评分仅反映信息完整度，
      不出具法律意见；建议结合律师/公证处专业意见
"""

from .models import Category, PlanScore, SubScore
from .scorer import PlanScorer

__all__ = ["Category", "PlanScore", "SubScore", "PlanScorer"]
