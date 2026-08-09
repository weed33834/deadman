"""DigitalLegacyGenerator - 数字遗产清单 → 可执行移交 / 注销方案

规则驱动为主（无 LLM 依赖，保证可离线运行、可测试），可选 LLM 增强生成自然语言说明。

设计要点：
    - 严守数据纪律：只产出通用步骤 + 引导查阅官方帮助中心，绝不编造深链 / 金额 / 电话。
    - 金额/估值仅回显用户自填内容，不替用户估算。
    - 与 knowledge/regions 的省级继承指引互补（本模块聚焦「数字资产」，非不动产/存款法条）。
"""

from __future__ import annotations

from typing import Any

from .models import (
    AssetAction,
    AssetCategory,
    AssetRegister,
    DigitalAsset,
)


def _action_label(action: str) -> str:
    return {
        AssetAction.TRANSFER.value: "转移给继承人",
        AssetAction.CLOSE.value: "注销 / 关闭",
        AssetAction.MEMORIALIZE.value: "转为纪念账号",
        AssetAction.KEEP.value: "保留（继续持有）",
        AssetAction.DECIDE.value: "待定（需与继承人商议）",
    }.get(action, "待定")


def _disposition_steps(asset: DigitalAsset) -> list[str]:
    """按类别 + 动作产出具体、可执行的后续步骤（不编造深链）。"""
    cat = asset.category
    action = asset.action_on_death
    steps: list[str] = []

    # 通用前置
    steps.append("收集该资产的标识信息（账号 / 邮箱 / 绑定手机），与死亡证明、亲属关系证明一并归档。")

    if cat == AssetCategory.CRYPTO.value:
        steps.append("定位私钥 / 助记词离线备份；切勿在线粘贴或截图。")
        if action in (AssetAction.TRANSFER.value, AssetAction.KEEP.value):
            steps.append("由指定继承人按钱包官方流程导入助记词接管；平台无法重置，务必提前演练。")
    elif cat == AssetCategory.FINANCIAL.value:
        steps.append("联系开户机构客服 / 网点，按官方继承流程提交材料（遗嘱、死亡证明、继承权公文书）。")
        steps.append("切勿共享登录密码；继承由机构核验身份后办理。")
    elif cat == AssetCategory.SOCIAL.value:
        if action == AssetAction.MEMORIALIZE.value:
            steps.append("在平台设置中指定遗产联系人 / 申请纪念化（如适用），或联系平台支持提交死亡证明。")
        elif action == AssetAction.CLOSE.value:
            steps.append("通过平台帮助中心的「账号注销 / 继承」流程提交申请，附死亡证明。")
    elif cat == AssetCategory.SUBSCRIPTION.value:
        steps.append("登录后取消自动续费 / 关闭订阅，避免逝者账户持续扣费。")
    elif cat == AssetCategory.DEVICE.value:
        steps.append("提前设置遗产联系人（如 Apple Legacy Contact、Google 遗嘱联系人），或离线记录解锁方式。")

    if action == AssetAction.DECIDE.value:
        steps.append("尚未决定处置方式：请与指定继承人确认后回填 action_on_death。")

    steps.append("具体材料清单与线上入口以各平台官方帮助中心为准，本指引不提供深链。")
    return steps


def build_checklist(reg: AssetRegister) -> dict[str, Any]:
    """规则驱动生成结构化清单（可序列化、可测试）。"""
    items: list[dict[str, Any]] = []
    for a in reg.assets:
        heir = reg.heir_by_id(a.assigned_heir_id)
        items.append(
            {
                "asset_id": a.id,
                "name": a.name,
                "category": a.category,
                "action": a.action_on_death,
                "action_label": _action_label(a.action_on_death),
                "assigned_heir": heir.name if heir else "（未指派）",
                "guidance": a.guidance,
                "steps": _disposition_steps(a),
                "sensitivity": a.sensitivity,
                "estimated_value": a.estimated_value,
            }
        )
    return {
        "user_id": reg.user_id,
        "summary": reg.summary(),
        "heirs": [h.to_dict() for h in reg.heirs],
        "items": items,
    }


def render_plan_markdown(reg: AssetRegister) -> str:
    """渲染为人类可读的数字遗产方案（Markdown）。"""
    chk = build_checklist(reg)
    lines: list[str] = []
    lines.append("# 数字遗产清单与处置方案\n")
    s = chk["summary"]
    lines.append(
        f"> 共登记 **{s['total_assets']}** 项数字资产，指派继承人 **{s['total_heirs']}** 位，"
        f"未指派 **{s['unassigned']}** 项。\n"
    )
    if chk["heirs"]:
        lines.append("## 继承人")
        for h in chk["heirs"]:
            rel = f"（{h['relationship']}）" if h.get("relationship") else ""
            lines.append(f"- {h['name']}{rel}")
        lines.append("")

    lines.append("## 资产与处置步骤")
    for it in chk["items"]:
        val = f"，估值：{it['estimated_value']}" if it["estimated_value"] else ""
        lines.append(f"### {it['name']} — {it['action_label']}（指派：{it['assigned_heir']}{val}）")
        lines.append("")
        lines.append(f"- 类别：{it['category']}｜敏感度：{it['sensitivity']}")
        lines.append(f"- 通用指引：{it['guidance']}")
        lines.append("- 后续步骤：")
        for st in it["steps"]:
            lines.append(f"  1. {st}")
        lines.append("")
    return "\n".join(lines)


async def generate_plan_llm(reg: AssetRegister, llm) -> str:
    """可选：用 LLM 把规则清单润色为自然语言说明（失败回退到规则渲染）。"""
    try:
        rule_md = render_plan_markdown(reg)
        prompt = (
            "你是身后事规划助手。下面是一份数字遗产清单的结构化方案，"
            "请用温和、专业、可执行的中文改写为面向继承人阅读的自然语言说明，"
            "保留所有资产名称、处置动作与指派关系，不要编造任何金额、电话或深链。\n\n"
            f"{rule_md}"
        )
        return await llm.chat([{"role": "user", "content": prompt}], max_tokens=1200)
    except Exception as exc:  # 网络 / 模型失败不阻断主体流程
        logger = __import__("logging").getLogger(__name__)
        logger.warning("LLM 增强生成失败，回退规则渲染: %s", exc)
        return render_plan_markdown(reg)
