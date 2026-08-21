"""To B 机构域基础测试（B2B-IMPLEMENTATION Step 2 验收）

覆盖：OrgStore（机构/成员）、InviteStore（邀请令牌）、rbac（角色权限）。
全部使用 tmp_path 隔离数据目录，不污染 ~/.deadman。
"""

from __future__ import annotations

import pytest

from deadman.org import InviteStore, OrgStore
from deadman.org.rbac import can, rank, require_rank


@pytest.fixture
def org_store(tmp_path):
    return OrgStore(data_dir=tmp_path / "org")


@pytest.fixture
def invite_store(tmp_path):
    return InviteStore(data_dir=tmp_path / "org", ttl_hours=24)


class TestOrgStore:
    def test_create_and_get(self, org_store):
        org = org_store.create_org("测试殡葬服务", slug="Funeral-A")
        assert org.org_id
        assert org.slug == "funeral-a"  # slug 小写归一
        assert org.industry_template == "funeral"
        got = org_store.get_org(org.org_id)
        assert got is not None
        assert got.name == "测试殡葬服务"

    def test_create_slug_unique(self, org_store):
        org_store.create_org("A", slug="dup")
        with pytest.raises(ValueError):
            org_store.create_org("B", slug="DUP")

    def test_create_validates_slug(self, org_store):
        with pytest.raises(ValueError):
            org_store.create_org("A", slug="")
        with pytest.raises(ValueError):
            org_store.create_org("A", slug="---")

    def test_get_by_slug(self, org_store):
        org_store.create_org("A", slug="funeral-x")
        found = org_store.get_org_by_slug("FUNERAL-X")
        assert found is not None
        assert found.name == "A"

    def test_update_org(self, org_store):
        org = org_store.create_org("A", slug="a")
        updated = org_store.update_org(
            org.org_id, name="A2", plan="pro", unknown_field="x"
        )
        assert updated is not None
        assert updated.name == "A2"
        assert updated.plan == "pro"
        # 白名单外字段被忽略，不影响已存数据
        assert not hasattr(updated, "unknown_field")

    def test_update_org_unknown(self, org_store):
        assert org_store.update_org("no-such", name="X") is None

    def test_update_org_status_validation(self, org_store):
        org = org_store.create_org("A", slug="a")
        with pytest.raises(ValueError):
            org_store.update_org(org.org_id, status="bogus")

    def test_list_and_delete(self, org_store):
        o1 = org_store.create_org("A", slug="a")
        o2 = org_store.create_org("B", slug="b")
        assert len(org_store.list_orgs()) == 2
        assert org_store.delete_org(o1.org_id)
        assert not org_store.delete_org(o1.org_id)
        assert [o.org_id for o in org_store.list_orgs()] == [o2.org_id]

    def test_is_active_with_expiry(self, org_store):
        org = org_store.create_org("A", slug="a")
        assert org.is_active()
        org_store.update_org(org.org_id, expires_at=0.0)
        assert not org_store.get_org(org.org_id).is_active()


class TestMembership:
    def test_add_and_get(self, org_store):
        org = org_store.create_org("A", slug="a")
        m = org_store.add_member(org.org_id, "u1", "case_manager")
        assert m.org_role == "case_manager"
        got = org_store.get_membership(org.org_id, "u1")
        assert got is not None
        assert got.user_id == "u1"
        assert got.status == "active"

    def test_add_member_unknown_org_raises(self, org_store):
        with pytest.raises(ValueError):
            org_store.add_member("no-such-org", "u1", "viewer")

    def test_add_member_role_validation(self, org_store):
        org = org_store.create_org("A", slug="a")
        with pytest.raises(ValueError):
            org_store.add_member(org.org_id, "u1", "superuser")

    def test_add_member_idempotent(self, org_store):
        org = org_store.create_org("A", slug="a")
        org_store.add_member(org.org_id, "u1", "viewer")
        # 重复添加升级角色
        m2 = org_store.add_member(org.org_id, "u1", "org_admin")
        assert m2.org_role == "org_admin"
        assert org_store.list_members(org.org_id)[0].org_role == "org_admin"

    def test_set_role_and_status(self, org_store):
        org = org_store.create_org("A", slug="a")
        org_store.add_member(org.org_id, "u1", "viewer")
        m = org_store.set_member_role(org.org_id, "u1", "case_manager")
        assert m.org_role == "case_manager"
        m2 = org_store.set_member_status(org.org_id, "u1", "disabled")
        assert m2.status == "disabled"
        assert org_store.set_member_role(org.org_id, "ghost", "viewer") is None

    def test_remove_member(self, org_store):
        org = org_store.create_org("A", slug="a")
        org_store.add_member(org.org_id, "u1", "viewer")
        assert org_store.remove_member(org.org_id, "u1")
        assert not org_store.remove_member(org.org_id, "u1")

    def test_list_user_orgs(self, org_store):
        o1 = org_store.create_org("A", slug="a")
        o2 = org_store.create_org("B", slug="b")
        org_store.add_member(o1.org_id, "u1", "org_admin")
        org_store.add_member(o2.org_id, "u1", "viewer")
        memberships = org_store.list_user_orgs("u1")
        assert len(memberships) == 2
        assert {m.org_id for m in memberships} == {o1.org_id, o2.org_id}

    def test_delete_org_removes_members(self, org_store):
        o1 = org_store.create_org("A", slug="a")
        org_store.add_member(o1.org_id, "u1", "viewer")
        org_store.delete_org(o1.org_id)
        assert org_store.list_members(o1.org_id) == []

    def test_delete_org_removes_invites(self, org_store, invite_store):
        o1 = org_store.create_org("A", slug="a")
        invite_store.create_invite(o1.org_id, "a@b.com")
        org_store.delete_org(o1.org_id)
        assert invite_store.list_invites(o1.org_id) == []

    def test_update_org_blank_name_rejected(self, org_store):
        org = org_store.create_org("A", slug="a")
        with pytest.raises(ValueError):
            org_store.update_org(org.org_id, name="")
        with pytest.raises(ValueError):
            org_store.update_org(org.org_id, name=None)


class TestInvite:
    def test_create_and_consume(self, invite_store):
        token = invite_store.create_invite("org1", "a@b.com", "case_manager")
        info = invite_store.consume_invite(token)
        assert info is not None
        assert info["org_id"] == "org1"
        assert info["email"] == "a@b.com"
        assert info["role"] == "case_manager"
        # 单次使用
        assert invite_store.consume_invite(token) is None

    def test_expired_invite(self, tmp_path):
        store = InviteStore(data_dir=tmp_path / "org", ttl_hours=-1)
        token = store.create_invite("org1", "a@b.com")
        assert store.consume_invite(token) is None

    def test_revoke_invite(self, invite_store):
        token = invite_store.create_invite("org1", "a@b.com")
        assert invite_store.revoke_invite(token)
        assert not invite_store.revoke_invite(token)
        assert invite_store.consume_invite(token) is None

    def test_list_invites_by_org(self, invite_store):
        invite_store.create_invite("org1", "a@b.com")
        invite_store.create_invite("org1", "c@d.com")
        invite_store.create_invite("org2", "e@f.com")
        assert len(invite_store.list_invites("org1")) == 2
        assert len(invite_store.list_invites("org2")) == 1

    def test_peek(self, invite_store):
        token = invite_store.create_invite("org1", "a@b.com", role="org_admin")
        info = invite_store.peek_invite(token)
        assert info["role"] == "org_admin"
        # peek 不消费
        assert invite_store.consume_invite(token) is not None

    def test_role_validation(self, invite_store):
        with pytest.raises(ValueError):
            invite_store.create_invite("org1", "a@b.com", role="hacker")

    def test_purge_all(self, invite_store):
        invite_store.create_invite("org1", "a@b.com")
        invite_store.create_invite("org2", "c@d.com")
        assert invite_store.purge_all() == 2
        assert invite_store.list_invites("org1") == []


class TestRbac:
    def test_rank(self):
        assert rank("viewer") == 0
        assert rank("consultant") == 1
        assert rank("case_manager") == 2
        assert rank("org_admin") == 3
        assert rank(None) == 0
        assert rank("unknown") == 0

    def test_can_matrix(self):
        # 查看（所有角色）
        assert can("viewer", "org.view")
        assert can("org_admin", "org.view")
        # 材料包：consultant+ 可生成
        assert not can("viewer", "org.material.generate")
        assert can("consultant", "org.material.generate")
        assert can("case_manager", "org.material.generate")
        # 案件：case_manager+
        assert not can("consultant", "org.case.manage")
        assert can("case_manager", "org.case.manage")
        assert can("org_admin", "org.case.manage")
        # 成员/审计/导出：仅 org_admin
        assert not can("case_manager", "org.members.manage")
        assert not can("case_manager", "org.audit.view")
        assert not can("case_manager", "org.export")
        assert can("org_admin", "org.export")
        # 未知动作拒绝
        assert not can("org_admin", "org.hack")

    def test_require_rank(self):
        predicate = require_rank("case_manager")
        assert predicate("org_admin")
        assert predicate("case_manager")
        assert not predicate("consultant")
        assert not predicate(None)
