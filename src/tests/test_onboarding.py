"""测试 deadman.onboarding - Phase 16C 新用户引导向导

覆盖点（>= 12 个）：
  - OnboardingWizard.get_step 5 步
  - validate_answer 各步骤（合法 + 非法）
  - save_profile 持久化
  - to_user_profile 转换
  - OnboardingStore save / load / delete
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from deadman.onboarding.models import OnboardingProfile
from deadman.onboarding.store import OnboardingStore
from deadman.onboarding.wizard import OnboardingWizard

# =====================================================================
# 1. get_step 5 步
# =====================================================================


class TestGetStep:
    def test_total_steps_is_5(self):
        wiz = OnboardingWizard()
        assert wiz.TOTAL_STEPS == 5
        assert wiz.STEPS == ["relationship", "location", "death_date", "current_stage", "consent"]

    def test_step_0_relationship(self):
        wiz = OnboardingWizard()
        step = wiz.get_step(0)
        assert step["key"] == "relationship"
        assert step["type"] == "select"
        assert "亲属" in step["options"]
        assert "朋友" in step["options"]
        assert "本人" in step["options"]
        assert "其他" in step["options"]
        assert step["required"] is True

    def test_step_1_location(self):
        wiz = OnboardingWizard()
        step = wiz.get_step(1)
        assert step["key"] == "location"
        assert step["type"] == "select"
        assert "北京" in step["options"]
        assert "上海" in step["options"]
        assert "海外" in step["options"]
        assert step["required"] is True

    def test_step_2_death_date(self):
        wiz = OnboardingWizard()
        step = wiz.get_step(2)
        assert step["key"] == "death_date"
        assert step["type"] == "date"
        assert step["required"] is False

    def test_step_3_current_stage(self):
        wiz = OnboardingWizard()
        step = wiz.get_step(3)
        assert step["key"] == "current_stage"
        assert step["type"] == "multiselect"
        assert step["required"] is False
        assert "死亡证明" in step["options"]

    def test_step_4_consent(self):
        wiz = OnboardingWizard()
        step = wiz.get_step(4)
        assert step["key"] == "consent"
        assert step["type"] == "checkbox"
        assert step["required"] is True
        assert "/terms" in step["links"]
        assert "/privacy" in step["links"]

    def test_step_out_of_range_raises(self):
        wiz = OnboardingWizard()
        with pytest.raises(ValueError, match="超出范围"):
            wiz.get_step(-1)
        with pytest.raises(ValueError, match="超出范围"):
            wiz.get_step(5)
        with pytest.raises(ValueError, match="超出范围"):
            wiz.get_step(99)


# =====================================================================
# 2. validate_answer 各步骤
# =====================================================================


class TestValidateAnswer:
    def test_relationship_valid(self):
        wiz = OnboardingWizard()
        for v in ["亲属", "朋友", "本人", "其他"]:
            ok, err = wiz.validate_answer(0, v)
            assert ok is True
            assert err == ""

    def test_relationship_invalid(self):
        wiz = OnboardingWizard()
        ok, err = wiz.validate_answer(0, "陌生人")
        assert ok is False
        assert "关系" in err
        ok, err = wiz.validate_answer(0, "")
        assert ok is False
        ok, err = wiz.validate_answer(0, None)
        assert ok is False

    def test_location_valid(self):
        wiz = OnboardingWizard()
        ok, _ = wiz.validate_answer(1, "北京")
        assert ok is True
        ok, _ = wiz.validate_answer(1, "海外")
        assert ok is True

    def test_location_invalid(self):
        wiz = OnboardingWizard()
        ok, err = wiz.validate_answer(1, "火星")
        assert ok is False
        assert "省份" in err

    def test_death_date_valid_iso(self):
        wiz = OnboardingWizard()
        ok, _ = wiz.validate_answer(2, "2024-01-15")
        assert ok is True

    def test_death_date_empty_allowed(self):
        wiz = OnboardingWizard()
        ok, _ = wiz.validate_answer(2, "")
        assert ok is True
        ok, _ = wiz.validate_answer(2, None)
        assert ok is True

    def test_death_date_future_rejected(self):
        wiz = OnboardingWizard()
        ok, err = wiz.validate_answer(2, "2099-01-01")
        assert ok is False
        assert "今天" in err

    def test_death_date_invalid_format(self):
        wiz = OnboardingWizard()
        ok, err = wiz.validate_answer(2, "not-a-date")
        assert ok is False
        assert "格式" in err

    def test_current_stage_empty_allowed(self):
        wiz = OnboardingWizard()
        ok, _ = wiz.validate_answer(3, [])
        assert ok is True
        ok, _ = wiz.validate_answer(3, None)
        assert ok is True

    def test_current_stage_valid_list(self):
        wiz = OnboardingWizard()
        ok, _ = wiz.validate_answer(3, ["死亡证明", "户口注销"])
        assert ok is True

    def test_current_stage_invalid_value(self):
        wiz = OnboardingWizard()
        ok, err = wiz.validate_answer(3, ["无效阶段"])
        assert ok is False
        assert "阶段" in err

    def test_current_stage_not_list(self):
        wiz = OnboardingWizard()
        ok, err = wiz.validate_answer(3, "死亡证明")
        assert ok is False
        assert "list" in err

    def test_consent_true(self):
        wiz = OnboardingWizard()
        ok, _ = wiz.validate_answer(4, True)
        assert ok is True

    def test_consent_false_rejected(self):
        wiz = OnboardingWizard()
        ok, err = wiz.validate_answer(4, False)
        assert ok is False
        assert "同意" in err

    def test_consent_not_bool(self):
        wiz = OnboardingWizard()
        ok, err = wiz.validate_answer(4, "yes")
        assert ok is False
        assert "布尔" in err


# =====================================================================
# 3. save_profile + OnboardingStore
# =====================================================================


class TestSaveProfile:
    def test_save_profile_persists_to_store(self, tmp_path: Path):
        store = OnboardingStore(data_dir=tmp_path)
        wiz = OnboardingWizard(store=store)
        profile = wiz.save_profile(
            "user-001",
            {
                "relationship": "亲属",
                "location": "北京",
                "death_date": "2024-01-15",
                "current_stage": ["死亡证明"],
                "consent": True,
            },
        )
        assert profile.user_id == "user-001"
        # 持久化
        loaded = store.load("user-001")
        assert loaded is not None
        assert loaded.relationship == "亲属"
        assert loaded.location == "北京"

    def test_save_profile_skip_death_date_when_self(self, tmp_path: Path):
        store = OnboardingStore(data_dir=tmp_path)
        wiz = OnboardingWizard(store=store)
        profile = wiz.save_profile(
            "user-002",
            {
                "relationship": "本人",
                "location": "上海",
                "death_date": "",  # 本人可跳过
                "current_stage": [],
                "consent": True,
            },
        )
        assert profile.death_date is None
        assert profile.relationship == "本人"

    def test_save_profile_missing_required_raises(self, tmp_path: Path):
        store = OnboardingStore(data_dir=tmp_path)
        wiz = OnboardingWizard(store=store)
        with pytest.raises(ValueError, match="必填字段缺失"):
            wiz.save_profile(
                "user-003",
                {
                    "relationship": "亲属",
                    # 缺 location
                    "death_date": "",
                    "current_stage": [],
                    "consent": True,
                },
            )

    def test_save_profile_consent_false_raises(self, tmp_path: Path):
        store = OnboardingStore(data_dir=tmp_path)
        wiz = OnboardingWizard(store=store)
        with pytest.raises(ValueError, match="consent"):
            wiz.save_profile(
                "user-004",
                {
                    "relationship": "亲属",
                    "location": "北京",
                    "death_date": "",
                    "current_stage": [],
                    "consent": False,
                },
            )

    def test_save_profile_invalid_user_id_raises(self, tmp_path: Path):
        store = OnboardingStore(data_dir=tmp_path)
        wiz = OnboardingWizard(store=store)
        with pytest.raises(ValueError, match="user_id"):
            wiz.save_profile(
                "",
                {
                    "relationship": "亲属",
                    "location": "北京",
                    "death_date": "",
                    "current_stage": [],
                    "consent": True,
                },
            )


# =====================================================================
# 4. OnboardingStore CRUD
# =====================================================================


class TestOnboardingStore:
    def test_load_nonexistent_returns_none(self, tmp_path: Path):
        store = OnboardingStore(data_dir=tmp_path)
        assert store.load("nonexistent") is None

    def test_delete_nonexistent_returns_false(self, tmp_path: Path):
        store = OnboardingStore(data_dir=tmp_path)
        assert store.delete("nonexistent") is False

    def test_save_load_delete_roundtrip(self, tmp_path: Path):
        store = OnboardingStore(data_dir=tmp_path)
        profile = OnboardingProfile(
            user_id="user-store-001",
            relationship="亲属",
            location="北京",
            death_date="2024-01-15",
            current_stage=["死亡证明", "户口注销"],
            consent_disclaimer=True,
        )
        store.save(profile)
        loaded = store.load("user-store-001")
        assert loaded is not None
        assert loaded.relationship == "亲属"
        assert loaded.location == "北京"
        assert loaded.death_date == "2024-01-15"
        assert loaded.current_stage == ["死亡证明", "户口注销"]
        assert loaded.consent_disclaimer is True
        # 删除
        ok = store.delete("user-store-001")
        assert ok is True
        assert store.load("user-store-001") is None

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX 文件权限位在 Windows 无语义")
    def test_file_permission_0o600(self, tmp_path: Path):
        store = OnboardingStore(data_dir=tmp_path)
        profile = OnboardingProfile(
            user_id="perm-test",
            relationship="朋友",
            location="上海",
            death_date=None,
            current_stage=[],
            consent_disclaimer=True,
        )
        store.save(profile)
        path = store._path_for("perm-test")
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, f"文件权限应为 0o600，实际 0o{mode:o}"

    def test_overwrite_on_resave(self, tmp_path: Path):
        store = OnboardingStore(data_dir=tmp_path)
        p1 = OnboardingProfile(
            user_id="overwrite-test",
            relationship="亲属",
            location="北京",
            death_date=None,
            current_stage=[],
            consent_disclaimer=True,
        )
        store.save(p1)
        # 再次保存（不同 relationship）
        p2 = OnboardingProfile(
            user_id="overwrite-test",
            relationship="朋友",
            location="上海",
            death_date=None,
            current_stage=[],
            consent_disclaimer=True,
        )
        store.save(p2)
        loaded = store.load("overwrite-test")
        assert loaded is not None
        assert loaded.relationship == "朋友"
        assert loaded.location == "上海"


# =====================================================================
# 5. to_user_profile 转换
# =====================================================================


class TestToUserProfile:
    def test_to_user_profile_contains_all_fields(self):
        profile = OnboardingProfile(
            user_id="u-001",
            relationship="亲属",
            location="北京",
            death_date="2024-01-15",
            current_stage=["死亡证明"],
            consent_disclaimer=True,
        )
        d = OnboardingWizard.to_user_profile(profile)
        assert d["relationship"] == "亲属"
        assert d["location"] == "北京"
        assert d["death_date"] == "2024-01-15"
        assert d["current_stage"] == ["死亡证明"]
        assert d["consent_disclaimer"] is True
        assert d["source"] == "onboarding_wizard"

    def test_to_user_profile_empty_current_stage(self):
        profile = OnboardingProfile(
            user_id="u-002",
            relationship="本人",
            location="海外",
            death_date=None,
            current_stage=[],
            consent_disclaimer=True,
        )
        d = OnboardingWizard.to_user_profile(profile)
        assert d["current_stage"] == []
        assert d["death_date"] is None
