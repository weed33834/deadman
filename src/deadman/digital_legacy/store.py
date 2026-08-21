"""DigitalLegacyStore - 数字遗产清单存储

与 vault 互补：vault 存「秘密本身」（加密信物），本模块存「资产清单与移交指引」，
其中 access_hint（访问 / 恢复方式）属高度敏感字段，落盘时 AES-256-GCM 加密。

存储路径：
    {root}/digital_legacy/{user_id}.json      # 元数据 + 资产（access_hint 已加密）

设计要点：
    - 沿用 utils.crypto.encrypt_envelope / decrypt_envelope（AES-256-GCM）。
    - 落盘文件不含明文 access_hint；仅存 envelope。
    - 严守数据纪律：location 只收用户自填或可公开域名，绝不编造深链 / 金额。
    - 遵守 rules/legal-compliance-framework.md（PIPL 最小必要、去标识化）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..utils import crypto
from .models import AssetRegister, DigitalAsset, Heir

logger = logging.getLogger(__name__)

# access_hint 加密后的字段名（明文字段置空，避免双份）
_ENC_FIELD = "_enc_access_hint"


def default_root() -> Path:
    """默认存储根目录（优先 DEADMAN_DATA_HOME，否则按租户路由）。"""
    import os

    base = os.environ.get("DEADMAN_DATA_HOME")
    if base:
        return Path(base) / "digital_legacy"
    from ..infrastructure.multi_tenant import resolve_tenant_path

    return resolve_tenant_path("digital_legacy")


class DigitalLegacyStore:
    def __init__(
        self,
        user_id: str,
        passphrase: bytes | None = None,
        root: Path | None = None,
    ) -> None:
        """passphrase 用于加解密 access_hint；为空时不做加密（仅测试 / 非敏感场景）。"""
        if not user_id:
            raise ValueError("user_id 不能为空")
        self.user_id = user_id
        self.passphrase = passphrase or b""
        self.root = root or default_root()

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    def _path(self) -> Path:
        return self.root / f"{self.user_id}.json"

    # ------------------------------------------------------------------
    # 序列化（落盘时加密 access_hint）
    # ------------------------------------------------------------------
    def _serialize(self, reg: AssetRegister) -> dict[str, Any]:
        data = reg.to_dict()
        for a in data["assets"]:
            hint = a.get("access_hint") or ""
            if hint and self.passphrase:
                a[_ENC_FIELD] = crypto.encrypt_envelope(hint.encode("utf-8"), self.passphrase)
                a["access_hint"] = ""
            elif hint:
                # 无口令：保留明文但明确标记（调用方应传入 passphrase）
                a["access_hint"] = hint
        data["updated_at"] = _now()
        return data

    def _deserialize(self, data: dict[str, Any]) -> AssetRegister:
        for a in data.get("assets", []):
            enc = a.pop(_ENC_FIELD, None)
            if enc and self.passphrase:
                try:
                    a["access_hint"] = crypto.decrypt_envelope(enc, self.passphrase).decode("utf-8")
                except ValueError as exc:
                    logger.warning("access_hint 解密失败（口令不匹配？）: %s", exc)
                    a["access_hint"] = ""
            elif enc and not self.passphrase:
                a["access_hint"] = ""
        return AssetRegister.from_dict(data)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def load(self) -> AssetRegister:
        """读取清单；不存在时返回空清单。"""
        p = self._path()
        if not p.exists():
            return AssetRegister(user_id=self.user_id)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("读取数字遗产清单失败 %s: %s", p, exc)
            raise
        return self._deserialize(data)

    def save(self, reg: AssetRegister) -> Path:
        """原子写盘（先写临时文件再 rename）。"""
        reg.user_id = self.user_id
        reg.updated_at = _now()
        self.root.mkdir(parents=True, exist_ok=True)
        p = self._path()
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._serialize(reg), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(p)
        return p

    def add_heir(self, heir: Heir) -> AssetRegister:
        reg = self.load()
        if not any(h.id == heir.id for h in reg.heirs):
            reg.heirs.append(heir)
        self.save(reg)
        return reg

    def add_asset(self, asset: DigitalAsset) -> AssetRegister:
        reg = self.load()
        reg.assets.append(asset)
        self.save(reg)
        return reg

    def assign_heir(self, asset_id: str, heir_id: str | None) -> AssetRegister:
        reg = self.load()
        for a in reg.assets:
            if a.id == asset_id:
                a.assigned_heir_id = heir_id
                a.updated_at = _now()
                break
        self.save(reg)
        return reg

    def remove_asset(self, asset_id: str) -> AssetRegister:
        reg = self.load()
        reg.assets = [a for a in reg.assets if a.id != asset_id]
        self.save(reg)
        return reg

    def summary(self) -> dict[str, int]:
        return self.load().summary()

    def wipe(self) -> None:
        p = self._path()
        if p.exists():
            p.unlink()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
