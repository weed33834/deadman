"""民俗 / 规则定制系统 —— 按地方民俗 + 自定义规则

围绕产品特色，提供"身后事 + 婚嫁等民俗"的规则定制：
  * 内置预置：常见地区丧葬民俗（流程/仪式/头七-七七）、婚嫁民俗
  * 自定义：用户可新增地区/类别/规则
  * 导入：按地区导入预置
  * 数据模型 Custom：{region, category, title, process[], rules[], weekly_observances[], notes}

端点：
  * GET    /api/customs                    —— 列表（可 ?region=&category=&q=）
  * POST   /api/customs                    —— 自定义新增
  * PUT    /api/customs/{id}               —— 更新
  * DELETE /api/customs/{id}               —— 删除
  * POST   /api/customs/import/{preset_id} —— 导入内置预置
  * GET    /api/customs/presets            —— 内置预置清单
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body

from ...errors import DeadmanHTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/customs", tags=["customs"])

_CUSTOMS_DIR = Path.home() / ".deadman" / "customs"


def _store() -> dict[str, Any]:
    p = _CUSTOMS_DIR / "customs.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save(data: dict[str, Any]) -> None:
    _CUSTOMS_DIR.mkdir(parents=True, exist_ok=True)
    (_CUSTOMS_DIR / "customs.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# =====================================================================
# 内置预置（示例性、可导入）
# =====================================================================

_PRESETS: list[dict[str, Any]] = [
    {
        "id": "funeral-cn-common",
        "region": "中国·通用",
        "category": "funeral",
        "title": "中国常见丧葬民俗（通用）",
        "process": [
            "开具死亡证明",
            "联系殡仪馆/安排停灵",
            "举行告别仪式",
            "出殡",
            "安葬/寄存",
            "头七至七七祭奠",
            "百日/周年祭",
        ],
        "rules": [
            {"title": "停灵", "detail": "一般停灵3天左右，部分地区停灵1-7天；需安排守灵。"},
            {"title": "告别仪式", "detail": "布置灵堂、花圈、挽联；亲友吊唁；司仪主持追思。"},
            {"title": "出殡", "detail": "择吉时出殡，长子/亲属执绋；灵车送行。"},
            {"title": "安葬", "detail": "土葬或火化后安葬骨灰；部分地区择日下葬。"},
        ],
        "weekly_observances": [
            {"day": "头七", "note": "死后第7天祭奠，最隆重。"},
            {"day": "二七", "note": "第14天祭奠。"},
            {"day": "三七", "note": "第21天，部分地区规模较大。"},
            {"day": "四七", "note": "第28天祭奠。"},
            {"day": "五七", "note": "第35天，部分地区亲友参加。"},
            {"day": "六七", "note": "第42天祭奠。"},
            {"day": "七七", "note": "第49天，圆满之日，大祭。"},
        ],
        "notes": "各地习俗差异大，以当地/家族习惯为准。",
    },
    {
        "id": "funeral-cn-north",
        "region": "中国·北方",
        "category": "funeral",
        "title": "北方常见丧葬民俗",
        "process": ["报丧", "停灵", "入殓", "出殡", "下葬", "烧七", "周年"],
        "rules": [
            {"title": "报丧", "detail": "亲属去世后向亲友报丧；北方多由孝子上门报信。"},
            {"title": "烧七", "detail": "头七至七七每七祭奠，逢单七（头/三/五/七七）较为重要。"},
        ],
        "weekly_observances": [],
        "notes": "北方部分地区重头七与三七。",
    },
    {
        "id": "funeral-cn-south",
        "region": "中国·南方",
        "category": "funeral",
        "title": "南方常见丧葬民俗",
        "process": ["报丧", "设灵堂", "停灵", "出殡", "安葬/寄存", "做七", "周年"],
        "rules": [
            {"title": "做七", "detail": "以头七至七七为主，部分地区做'五七'最重。"},
            {"title": "风水", "detail": "南方部分地区重视风水择地安葬。"},
        ],
        "weekly_observances": [],
        "notes": "南方沿海部分地区有二次捡骨等习俗。",
    },
    {
        "id": "wedding-cn-common",
        "region": "中国·通用",
        "category": "wedding",
        "title": "中国常见婚嫁民俗（通用）",
        "process": ["提亲", "订婚", "纳彩/彩礼", "选吉日", "迎亲", "婚礼仪式", "回门"],
        "rules": [
            {"title": "迎亲", "detail": "男方至女方家迎亲，堵门/找鞋等习俗。"},
            {"title": "婚礼仪式", "detail": "拜堂/宣誓/敬茶等；各地流程差异大。"},
            {"title": "回门", "detail": "婚后第3天或择日回娘家。"},
        ],
        "weekly_observances": [],
        "notes": "各民族、地区婚俗差异大。",
    },
]


# =====================================================================
# 端点
# =====================================================================


@router.get("/presets")
async def customs_presets() -> dict[str, Any]:
    """GET /api/customs/presets —— 内置预置清单"""
    return {
        "ok": True,
        "presets": [
            {"id": p["id"], "region": p["region"], "category": p["category"], "title": p["title"]}
            for p in _PRESETS
        ],
    }


@router.get("")
async def customs_list(region: str = "", category: str = "", q: str = "") -> dict[str, Any]:
    """GET /api/customs —— 列表（可按地区/类别/关键词过滤）"""
    items = [dict(v, id=k) for k, v in _store().items() if isinstance(v, dict)]
    if region:
        items = [i for i in items if region in i.get("region", "")]
    if category:
        items = [i for i in items if i.get("category") == category]
    if q:
        ql = q.lower()
        items = [
            i
            for i in items
            if ql in i.get("title", "").lower()
            or any(r.get("title", "") in q for r in i.get("rules", []))
        ]
    items.sort(key=lambda x: x.get("region", ""))
    return {"ok": True, "customs": items, "count": len(items)}


@router.post("")
async def customs_create(custom: dict[str, Any] = Body(default=None)) -> dict[str, Any]:  # noqa: B008
    """POST /api/customs —— 自定义新增民俗规则"""
    custom = custom or {}
    if not custom.get("region") or not custom.get("title"):
        raise DeadmanHTTPException("DM-VALID-4002", message="region 与 title 必填")
    cid = custom.get("id") or f"custom-{uuid.uuid4().hex[:10]}"
    store = _store()
    store[cid] = {
        "region": custom["region"],
        "category": custom.get("category", "custom"),
        "title": custom["title"],
        "process": custom.get("process", []),
        "rules": custom.get("rules", []),
        "weekly_observances": custom.get("weekly_observances", []),
        "notes": custom.get("notes", ""),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save(store)
    return {"ok": True, "custom": store[cid]}


@router.put("/{custom_id}")
async def customs_update(
    custom_id: str, custom: dict[str, Any] = Body(default=None)  # noqa: B008
) -> dict[str, Any]:
    """PUT /api/customs/{id} —— 更新民俗规则"""
    store = _store()
    if custom_id not in store:
        raise DeadmanHTTPException("DM-GENERAL-4040", message=f"民俗规则不存在: {custom_id}")
    existing = store[custom_id]
    for k in ("region", "category", "title", "process", "rules", "weekly_observances", "notes"):
        if k in custom:
            existing[k] = custom[k]
    store[custom_id] = existing
    _save(store)
    return {"ok": True, "custom": existing}


@router.delete("/{custom_id}")
async def customs_delete(custom_id: str) -> dict[str, Any]:
    """DELETE /api/customs/{id} —— 删除民俗规则"""
    store = _store()
    if custom_id in store:
        del store[custom_id]
        _save(store)
        return {"ok": True, "custom_id": custom_id, "deleted": True}
    raise DeadmanHTTPException("DM-GENERAL-4040", message=f"民俗规则不存在: {custom_id}")


@router.post("/import/{preset_id}")
async def customs_import(preset_id: str) -> dict[str, Any]:
    """POST /api/customs/import/{id} —— 导入内置预置"""
    preset = next((p for p in _PRESETS if p["id"] == preset_id), None)
    if preset is None:
        raise DeadmanHTTPException("DM-GENERAL-4040", message=f"预置不存在: {preset_id}")
    store = _store()
    store[preset_id] = dict(preset, created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    _save(store)
    return {"ok": True, "custom": store[preset_id]}
