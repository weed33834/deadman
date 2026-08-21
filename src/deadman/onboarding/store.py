"""Onboarding 画像存储 - Phase 16C

参考 auth/store.py / support/store.py 原子写入模式：
- 路径：~/.deadman/onboarding/{user_id}.json（权限 0o600）
- 原子写入：先 .tmp 再 os.replace
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from .models import OnboardingProfile

# 模块级默认目录（测试 monkeypatch 重定向用；None=按租户路由）
_DEFAULT_DATA_DIR: Path | None = None


def _default_data_dir() -> Path:
    """默认数据目录：优先环境变量，其次测试重定向的模块级常量，否则按租户路由。"""
    env_dir = os.getenv("DEADMAN_ONBOARDING_DATA_DIR")
    if env_dir:
        return Path(env_dir)
    if _DEFAULT_DATA_DIR is not None:
        return _DEFAULT_DATA_DIR
    from ..infrastructure.multi_tenant import resolve_tenant_path

    return resolve_tenant_path("onboarding")


class OnboardingStore:
    """Onboarding 画像存储 - per-user 单文件

    存储结构：
        ~/.deadman/onboarding/
        ├── {user_id_1}.json
        └── {user_id_2}.json

    每个 user_id 对应一个画像文件（覆盖写）。
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir: Path = Path(data_dir) if data_dir else _default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.data_dir, 0o700)

    # ============================================================
    # 公开 API
    # ============================================================

    def save(self, profile: OnboardingProfile) -> None:
        """保存或更新画像（覆盖写）"""
        path = self._path_for(profile.user_id)
        tmp_path = path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(
                json.dumps(profile.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, path)
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise

    def load(self, user_id: str) -> OnboardingProfile | None:
        """加载画像，不存在返回 None"""
        path = self._path_for(user_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return OnboardingProfile.from_dict(data)
        except (json.JSONDecodeError, OSError, KeyError):
            return None

    def delete(self, user_id: str) -> bool:
        """删除画像，存在并删除成功返回 True；不存在返回 False"""
        path = self._path_for(user_id)
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError:
            return False

    # ============================================================
    # 内部
    # ============================================================

    def _path_for(self, user_id: str) -> Path:
        # user_id 是 UUID，安全作为文件名；保险起见替换任何特殊字符
        safe = "".join(c if c.isalnum() or c in {"-", "_"} else "_" for c in user_id)
        return self.data_dir / f"{safe}.json"
