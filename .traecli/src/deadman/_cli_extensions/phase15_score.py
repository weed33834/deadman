"""Phase 15 CLI 集成清单 - 身后事规划完整度评分（plan_score）

参考竞品：Trust & Will EstateOS 的 Plan Strength Score
deadman 差异化：综合 5 维度评分 + 缺失项清单 + top-3 智能建议

本模块定义 2 个 CLI 子命令实现 + subparser 注册函数。
不修改 `deadman.cli.main()`；调用方按需 import 后挂载到自己的 subparsers。

subparser 清单：
    plan-score         --user-id STR（或 --token STR）  计算并显示规划完整度评分
    plan-score-detail  --user-id STR                    显示每个类别的详细分数 + 缺失项 + 建议

合规关联：
    - integrity-framework.md L1：评分基于实际加载到的数据，不编造
    - service-boundary-framework.md L3：评分仅作参考，不出具法律意见
      所有输出末尾附 disclaimer
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from ..plan_score.scorer import PlanScorer

_DISCLAIMER = (
    "【边界告知】评分仅反映信息完整度，不代表法律效力；"
    "建议结合律师/公证处专业意见。"
)


# ====================================================================
# 子命令实现
# ====================================================================


def _resolve_user_id(args: argparse.Namespace) -> str:
    """从 --user-id 或 --token 解析出 user_id

    --token 优先：解析 JWT 拿 user_id（与 phase8 cmd_auth_me 一致）
    """
    # 优先 --token
    token = getattr(args, "token", None)
    if token:
        from ..auth.jwt import JWTManager
        from ..config import settings

        payload = JWTManager(
            secret=settings.jwt_secret or None,
            expiry_days=settings.jwt_expiry_days,
        ).verify(token)
        if not payload:
            raise SystemExit("token 无效或过期")
        return payload.get("user_id", "")

    user_id = getattr(args, "user_id", None)
    if not user_id:
        raise SystemExit("必须提供 --user-id 或 --token")
    return user_id


def cmd_plan_score(args: argparse.Namespace) -> None:
    """plan-score --user-id STR（或 --token STR）

    计算并显示规划完整度评分（含总分 + 5 维度分数 + top-3 建议）。
    """
    user_id = _resolve_user_id(args)
    scorer = PlanScorer()
    result = scorer.score(user_id)

    print("=== 身后事规划完整度评分 ===")
    print(f"user_id:       {result.user_id}")
    print(f"total_score:   {result.total_score}/100")
    print(f"generated_at:  {result.generated_at.isoformat()}")
    print()
    print("--- 各维度分数 ---")
    for sub in result.category_scores:
        print(
            f"  {sub.category.value:<18} {sub.score:>3}/100  "
            f"完成 {len(sub.completed_items)} 项 / 缺失 {len(sub.missing_items)} 项"
        )
    print()
    print("--- top 3 优先建议 ---")
    if result.overall_suggestions:
        for i, s in enumerate(result.overall_suggestions, 1):
            print(f"  {i}. {s}")
    else:
        print("  （无建议，所有维度均已完成）")
    print()
    print(_DISCLAIMER)


def cmd_plan_score_detail(args: argparse.Namespace) -> None:
    """plan-score-detail --user-id STR

    显示每个类别的详细分数 + 已完成项 + 缺失项 + 维度建议。
    输出 JSON 格式（便于脚本解析）+ 人类可读摘要。
    """
    user_id = _resolve_user_id(args)
    scorer = PlanScorer()
    result = scorer.score(user_id)

    # 人类可读摘要
    print("=== 身后事规划完整度评分（详细）===")
    print(f"user_id:       {result.user_id}")
    print(f"total_score:   {result.total_score}/100")
    print(f"generated_at:  {result.generated_at.isoformat()}")
    print()
    for sub in result.category_scores:
        print(f"================ {sub.category.value} ================")
        print(f"score: {sub.score}/100")
        print()
        print(f"已完成项 ({len(sub.completed_items)}):")
        for item in sub.completed_items:
            print(f"  ✓ {item}")
        if not sub.completed_items:
            print("  （无）")
        print()
        print(f"缺失项 ({len(sub.missing_items)}):")
        for item in sub.missing_items:
            print(f"  ✗ {item}")
        if not sub.missing_items:
            print("  （无）")
        print()
        print(f"维度建议 ({len(sub.suggestions)}):")
        for i, s in enumerate(sub.suggestions, 1):
            print(f"  {i}. {s}")
        if not sub.suggestions:
            print("  （无）")
        print()
    print("================ 跨维度 top 3 建议 ================")
    if result.overall_suggestions:
        for i, s in enumerate(result.overall_suggestions, 1):
            print(f"  {i}. {s}")
    else:
        print("  （无）")
    print()
    print(_DISCLAIMER)

    # JSON 输出（便于脚本解析）
    print()
    print("--- JSON ---")
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


# ====================================================================
# subparser 注册（供调用方按需挂载）
# ====================================================================


def register_subparsers(subparsers: Any) -> None:
    """把 Phase 15 的 2 个子命令挂载到 subparsers。

    用法：
        from deadman._cli_extensions import phase15_score
        phase15_score.register_subparsers(subparsers)
    """
    # plan-score
    score_parser = subparsers.add_parser(
        "plan-score",
        help="计算身后事规划完整度评分（Phase 15）",
        description="综合 5 维度评分 + 缺失项清单 + top-3 智能建议",
    )
    score_parser.add_argument(
        "--user-id", default=None, help="用户 ID（与 --token 二选一）"
    )
    score_parser.add_argument(
        "--token", default=None, help="JWT token（与 --user-id 二选一）"
    )
    score_parser.set_defaults(func=cmd_plan_score)

    # plan-score-detail
    detail_parser = subparsers.add_parser(
        "plan-score-detail",
        help="显示规划完整度评分详细分解（Phase 15）",
        description="每个类别的详细分数 + 已完成项 + 缺失项 + 建议",
    )
    detail_parser.add_argument(
        "--user-id", default=None, help="用户 ID（与 --token 二选一）"
    )
    detail_parser.add_argument(
        "--token", default=None, help="JWT token（与 --user-id 二选一）"
    )
    detail_parser.set_defaults(func=cmd_plan_score_detail)


# 命令清单（供 cli.py 引用）
COMMANDS = [
    "plan-score",
    "plan-score-detail",
]
