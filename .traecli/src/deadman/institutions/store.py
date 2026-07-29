"""殡葬机构存储 - 纯文件，可被 web_search 增量更新

存储路径：~/.deadman/institutions/institutions.json
遵守 retrieval-guardrails.md：
- confidence < 0.5 的机构数据输出时必须提示"建议向官方核实"
- 每条数据必须有 source 字段说明数据来源
- 缺失来源的条目按"不可信"处理

不存储用户亲属逝者信息（PII 风险），只存公开机构信息。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


# === 默认存储路径 ===
_DEFAULT_DATA_DIR = Path.home() / ".deadman" / "institutions"

# === 种子数据路径（包内自带，首次启动时加载）===
_SEED_FILE = Path(__file__).resolve().parent.parent.parent.parent / "knowledge" / "institutions" / "seed.json"

# === 类型枚举 ===
VALID_TYPES = {
    "funeral_home",        # 殡仪馆
    "crematorium",         # 火化场
    "cemetery",            # 公墓
    "funeral_service_station",  # 殡仪服务站
}


@dataclass
class Institution:
    """殡葬机构 - 数据来自公开政务平台

    字段说明：
    - institution_id: 内部生成的唯一 ID（uuid4 前 12 位）
    - confidence: 数据可信度 0.0-1.0
        - >=0.7: 中可信（1 个官方源或多个非官方源一致）
        - >=0.5: 低可信（单一非官方源）
        - <0.5: 不可信，输出时必须提示"建议向官方核实"
    - source: 数据来源（如"山东省民政厅 2026.6"），缺失视为不可信
    """
    institution_id: str
    name: str
    type: str  # funeral_home / crematorium / cemetery / funeral_service_station
    province: str
    city: str
    district: str | None = None
    address: str | None = None
    phone: str | None = None
    services: list[str] = field(default_factory=list)
    price_public: bool = False  # 是否明码标价
    source: str = ""
    confidence: float = 0.7
    updated_at: datetime = field(default_factory=datetime.now)

    def __post_init__(self) -> None:
        if self.type not in VALID_TYPES:
            raise ValueError(
                f"未知机构类型: {self.type}，可选值: {sorted(VALID_TYPES)}"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence 必须在 [0.0, 1.0] 区间，当前: {self.confidence}")
        if not self.source:
            # retrieval-guardrails: 缺失来源按不可信处理，强制降级
            self.confidence = min(self.confidence, 0.4)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict（updated_at 转 ISO 字符串）"""
        d = asdict(self)
        d["updated_at"] = self.updated_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Institution":
        """从 dict 反序列化（兼容 updated_at 为字符串或 datetime）"""
        data = dict(data)  # 浅拷贝避免修改入参
        ua = data.get("updated_at")
        if isinstance(ua, str):
            try:
                data["updated_at"] = datetime.fromisoformat(ua)
            except ValueError:
                data["updated_at"] = datetime.now()
        elif ua is None:
            data["updated_at"] = datetime.now()
        return cls(**data)

    def needs_verification_warning(self) -> bool:
        """是否需要输出"建议向官方核实"警告

        依据 retrieval-guardrails.md：
        - confidence < 0.5（不可信）：必须提示
        - 0.5 <= confidence < 0.7（低可信）：建议提示
        - >=0.7（中/高可信）：可选
        本方法对 <0.7 一律提示，保守起见。
        """
        return self.confidence < 0.7


def _gen_id() -> str:
    """生成 12 位 institution_id"""
    return uuid.uuid4().hex[:12]


class InstitutionStore:
    """机构存储 - 纯文件 JSON

    存储路径：~/.deadman/institutions/institutions.json
    可被 web_search 工具增量更新（add/import_from_official_source）。
    首次初始化时自动加载包内 seed.json。
    """

    def __init__(
        self,
        data_dir: Path | None = None,
        auto_load_seed: bool = True,
    ) -> None:
        """初始化机构存储

        参数：
        - data_dir: 存储目录，默认 ~/.deadman/institutions
        - auto_load_seed: 首次启动且存储为空时，是否自动加载包内 seed.json。
          生产环境默认 True；测试需要空 store 时传 False。
        """
        self.data_dir: Path = data_dir if data_dir is not None else _DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store_file: Path = self.data_dir / "institutions.json"
        self._institutions: dict[str, Institution] = {}
        self._load()
        # 首次启动且存储为空时，从包内 seed.json 加载
        if auto_load_seed and not self._institutions and _SEED_FILE.exists():
            self._load_seed()

    # === 内部加载/保存 ===

    def _load(self) -> None:
        if not self.store_file.exists():
            return
        try:
            data = json.loads(self.store_file.read_text(encoding="utf-8"))
            for item in data.get("institutions", []):
                try:
                    inst = Institution.from_dict(item)
                    self._institutions[inst.institution_id] = inst
                except Exception:
                    # 跳过损坏条目，不抛异常（韧性优先）
                    continue
        except (json.JSONDecodeError, OSError):
            return

    def _load_seed(self) -> None:
        """从包内 seed.json 加载种子数据"""
        try:
            data = json.loads(_SEED_FILE.read_text(encoding="utf-8"))
            count = 0
            for item in data.get("institutions", []):
                try:
                    inst = Institution.from_dict(item)
                    # 去重：name + address
                    if self._find_duplicate(inst) is None:
                        self._institutions[inst.institution_id] = inst
                        count += 1
                except Exception:
                    continue
            if count:
                self._save()
        except (json.JSONDecodeError, OSError):
            return

    def _save(self) -> None:
        payload = {
            "institutions": [inst.to_dict() for inst in self._institutions.values()],
            "updated_at": datetime.now().isoformat(),
        }
        # 原子写入：先写 .tmp 再 rename
        tmp = self.store_file.with_suffix(self.store_file.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.store_file)

    def _find_duplicate(self, inst: Institution) -> str | None:
        """按 name + address 去重，返回已存在的 institution_id 或 None"""
        for existing in self._institutions.values():
            if existing.name == inst.name and (existing.address or "") == (inst.address or ""):
                return existing.institution_id
        return None

    # === 公开 API ===

    def add(self, inst: Institution) -> None:
        """添加机构（去重 by name+address）

        如已存在同名同地址机构，更新其字段（保留原 institution_id）。
        """
        existing_id = self._find_duplicate(inst)
        if existing_id is not None:
            # 合并更新：以新值覆盖旧值
            existing = self._institutions[existing_id]
            existing.name = inst.name
            existing.type = inst.type
            existing.province = inst.province
            existing.city = inst.city
            existing.district = inst.district
            existing.address = inst.address
            existing.phone = inst.phone
            existing.services = inst.services
            existing.price_public = inst.price_public
            existing.source = inst.source
            existing.confidence = max(existing.confidence, inst.confidence)
            existing.updated_at = datetime.now()
        else:
            self._institutions[inst.institution_id] = inst
        self._save()

    def search(
        self,
        province: str | None = None,
        city: str | None = None,
        type: str | None = None,
        keyword: str | None = None,
    ) -> list[Institution]:
        """搜索机构

        所有参数为可选，未指定则不过滤。
        keyword 在 name / address / services 中模糊匹配（大小写不敏感）。
        """
        results: list[Institution] = []
        for inst in self._institutions.values():
            if province is not None and inst.province != province:
                continue
            if city is not None and inst.city != city:
                continue
            if type is not None and inst.type != type:
                continue
            if keyword is not None:
                kw = keyword.lower()
                haystack = " ".join([
                    inst.name,
                    inst.address or "",
                    " ".join(inst.services),
                ]).lower()
                if kw not in haystack:
                    continue
            results.append(inst)
        # 按 province -> city -> name 排序，结果稳定
        results.sort(key=lambda x: (x.province, x.city, x.name))
        return results

    def get(self, institution_id: str) -> Institution | None:
        return self._institutions.get(institution_id)

    def update(self, institution_id: str, updates: dict) -> Institution | None:
        """更新机构字段

        updates 是 dict，键为 Institution 字段名，值为新值。
        不允许更新 institution_id 本身。
        """
        inst = self._institutions.get(institution_id)
        if inst is None:
            return None
        for key, value in updates.items():
            if key == "institution_id":
                continue
            if hasattr(inst, key):
                # type 字段需校验
                if key == "type" and value not in VALID_TYPES:
                    raise ValueError(f"未知机构类型: {value}")
                if key == "confidence" and not 0.0 <= value <= 1.0:
                    raise ValueError("confidence 必须在 [0.0, 1.0] 区间")
                setattr(inst, key, value)
        inst.updated_at = datetime.now()
        self._save()
        return inst

    def delete(self, institution_id: str) -> bool:
        if institution_id not in self._institutions:
            return False
        del self._institutions[institution_id]
        self._save()
        return True

    def count(self) -> int:
        return len(self._institutions)

    def import_from_official_source(self, source_name: str, data: list[dict]) -> int:
        """从官方数据源批量导入

        如山东省 117 家殡仪馆。source_name 会写入每条记录的 source 字段。
        返回新增条数（去重后）。
        """
        added = 0
        for item in data:
            # 复制避免修改入参
            item = dict(item)
            item.setdefault("institution_id", _gen_id())
            item["source"] = source_name
            item.setdefault("confidence", 0.7)
            try:
                inst = Institution.from_dict(item)
            except Exception:
                continue
            existing_id = self._find_duplicate(inst)
            if existing_id is not None:
                # 已存在则更新 source/confidence（以更高者为准）
                existing = self._institutions[existing_id]
                existing.source = source_name
                existing.confidence = max(existing.confidence, inst.confidence)
                existing.updated_at = datetime.now()
                continue
            self._institutions[inst.institution_id] = inst
            added += 1
        if added:
            self._save()
        return added


def make_institution(**kwargs) -> Institution:
    """便捷工厂函数：自动生成 institution_id"""
    kwargs.setdefault("institution_id", _gen_id())
    return Institution(**kwargs)
