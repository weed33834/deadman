"""测试 deadman.vault.store - 数字遗产保险库

覆盖：
    - owner 能获取自己条目
    - beneficiary 只能获取被指定的
    - 无权限返回 None
    - 文件中无明文 content
    - 列表返回不含 content
    - 死亡触发有 7 天等待
    - 手动立即投递
    - 列出受益人
    - 列出我能继承的
    - 删除成功

测试隔离：每个测试用 tmp_path 独立目录。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path


from deadman.vault.store import (
    TRIGGER_MANUAL,
    TRIGGER_ON_DATE,
    TRIGGER_ON_DEATH,
    ON_DEATH_WAIT_DAYS,
    VaultItem,
    VaultStore,
)


# =====================================================================
# 辅助：构造独立 store
# =====================================================================
def _make_store(tmp_path: Path) -> VaultStore:
    return VaultStore(data_dir=tmp_path / "vault")


# =====================================================================
# 1. owner 能获取自己条目
# =====================================================================
def test_add_and_get_item_owner(tmp_path: Path):
    store = _make_store(tmp_path)
    item = store.add_item(
        owner_user_id="u-owner",
        type="password",
        title="邮箱密码",
        content="my-secret-password-123",
        beneficiary_user_ids=["u-bene"],
    )
    assert isinstance(item, VaultItem)
    assert item.item_id.startswith("item-")
    assert item.owner_user_id == "u-owner"
    assert item.title == "邮箱密码"
    # owner 调 get_item 应能拿到
    fetched = store.get_item(item.item_id, "u-owner")
    assert fetched is not None
    assert fetched.item_id == item.item_id
    assert fetched.beneficiary_user_ids == ["u-bene"]


# =====================================================================
# 2. beneficiary 只能获取被指定的条目
# =====================================================================
def test_get_item_beneficiary_only_sees_shared(tmp_path: Path):
    store = _make_store(tmp_path)
    # 给 u-bene 指定一条
    item1 = store.add_item(
        owner_user_id="u-owner",
        type="note",
        title="给儿子的信",
        content="son-secret",
        beneficiary_user_ids=["u-bene"],
    )
    # 给其他人指定另一条（u-bene 不应能拿）
    item2 = store.add_item(
        owner_user_id="u-owner",
        type="note",
        title="给女儿的信",
        content="daughter-secret",
        beneficiary_user_ids=["u-other-bene"],
    )
    # u-bene 能拿到 item1
    fetched1 = store.get_item(item1.item_id, "u-bene")
    assert fetched1 is not None
    assert fetched1.item_id == item1.item_id
    # u-bene 不能拿到 item2
    fetched2 = store.get_item(item2.item_id, "u-bene")
    assert fetched2 is None


# =====================================================================
# 3. 无权限返回 None
# =====================================================================
def test_get_item_unauthorized_returns_none(tmp_path: Path):
    store = _make_store(tmp_path)
    item = store.add_item(
        owner_user_id="u-owner",
        type="note",
        title="私人笔记",
        content="private",
        beneficiary_user_ids=["u-bene"],
    )
    # 完全无关的用户
    fetched = store.get_item(item.item_id, "u-stranger")
    assert fetched is None


# =====================================================================
# 4. 文件中无明文 content
# =====================================================================
def test_content_encrypted_at_rest(tmp_path: Path):
    store = _make_store(tmp_path)
    secret = "my-super-secret-content-12345"
    item = store.add_item(
        owner_user_id="u-owner",
        type="password",
        title="密码",
        content=secret,
        beneficiary_user_ids=["u-bene"],
    )
    # 读取加密文件
    enc_path = store._item_file("u-owner", item.item_id)
    assert enc_path.exists()
    enc_bytes = enc_path.read_bytes()
    # 明文不应出现在加密文件里
    assert secret.encode("utf-8") not in enc_bytes
    # 索引文件也不应含明文 content
    index = store._read_index("u-owner")
    assert "content_encrypted" not in index[item.item_id]
    assert "content" not in index[item.item_id]
    # 解密后应等于原文
    key = store._derive_key("u-owner", store._get_master_password())
    plaintext = store._decrypt(enc_bytes, key)
    assert plaintext.decode("utf-8") == secret


# =====================================================================
# 5. 列表返回不含 content
# =====================================================================
def test_list_items_metadata_only(tmp_path: Path):
    store = _make_store(tmp_path)
    store.add_item(
        owner_user_id="u-owner",
        type="note",
        title="条目1",
        content="content-1",
        beneficiary_user_ids=["u-bene"],
    )
    store.add_item(
        owner_user_id="u-owner",
        type="note",
        title="条目2",
        content="content-2",
        beneficiary_user_ids=["u-bene"],
    )
    items = store.list_items("u-owner", "u-owner")
    assert len(items) == 2
    for entry in items:
        # 索引条目不应含 content_encrypted 字段
        assert "content_encrypted" not in entry
        # 但应含 title / type / item_id 等元数据
        assert "title" in entry
        assert "type" in entry
        assert "item_id" in entry


# =====================================================================
# 6. 死亡触发有 7 天等待期
# =====================================================================
def test_trigger_on_death_has_7day_wait(tmp_path: Path):
    store = _make_store(tmp_path)
    item = store.add_item(
        owner_user_id="u-owner",
        type="note",
        title="身后交付",
        content="after-death-secret",
        beneficiary_user_ids=["u-bene"],
        delivery_trigger=TRIGGER_ON_DEATH,
    )
    # owner 触发 on_death → 进入等待期
    r1 = store.trigger_delivery(item.item_id, TRIGGER_ON_DEATH, "u-owner")
    assert r1["delivered"] is False
    assert r1["pending_days"] == ON_DEATH_WAIT_DAYS
    assert r1["reason"] == "death_wait_started"
    # 受益人立刻触发 → 仍在等待期
    r2 = store.trigger_delivery(item.item_id, TRIGGER_ON_DEATH, "u-bene")
    assert r2["delivered"] is False
    assert r2["pending_days"] >= 1
    assert r2["reason"] == "in_death_wait_period"


# =====================================================================
# 7. 手动立即投递
# =====================================================================
def test_trigger_manual_delivers_immediately(tmp_path: Path):
    store = _make_store(tmp_path)
    secret = "manual-deliver-content"
    item = store.add_item(
        owner_user_id="u-owner",
        type="note",
        title="手动交付",
        content=secret,
        beneficiary_user_ids=["u-bene"],
        delivery_trigger=TRIGGER_MANUAL,
    )
    # 受益人触发 manual 立即投递
    r = store.trigger_delivery(item.item_id, TRIGGER_MANUAL, "u-bene")
    assert r["delivered"] is True
    assert r["content"] is not None
    assert r["content"].decode("utf-8") == secret
    assert r["reason"] == "delivered"


# =====================================================================
# 8. 列出我指定的受益人
# =====================================================================
def test_list_beneficiaries(tmp_path: Path):
    store = _make_store(tmp_path)
    store.add_item(
        owner_user_id="u-owner",
        type="note",
        title="a",
        content="x",
        beneficiary_user_ids=["u-bene-1", "u-bene-2"],
    )
    store.add_item(
        owner_user_id="u-owner",
        type="note",
        title="b",
        content="y",
        beneficiary_user_ids=["u-bene-1"],
    )
    beneficiaries = store.list_beneficiaries("u-owner")
    ids = [b["beneficiary_user_id"] for b in beneficiaries]
    assert "u-bene-1" in ids
    assert "u-bene-2" in ids
    # u-bene-1 被指定 2 次
    for b in beneficiaries:
        if b["beneficiary_user_id"] == "u-bene-1":
            assert b["item_count"] == 2
        if b["beneficiary_user_id"] == "u-bene-2":
            assert b["item_count"] == 1


# =====================================================================
# 9. 列出我能继承的
# =====================================================================
def test_list_inherited(tmp_path: Path):
    store = _make_store(tmp_path)
    # u-owner 给 u-bene 指定 2 条
    store.add_item(
        owner_user_id="u-owner",
        type="note",
        title="继承1",
        content="x",
        beneficiary_user_ids=["u-bene"],
        delivery_trigger=TRIGGER_MANUAL,
    )
    store.add_item(
        owner_user_id="u-owner",
        type="note",
        title="继承2-on_date",
        content="y",
        beneficiary_user_ids=["u-bene"],
        delivery_trigger=TRIGGER_ON_DATE,
        delivery_date=datetime.utcnow() + timedelta(days=30),
    )
    # 另一个 owner 也给 u-bene 指定一条
    store.add_item(
        owner_user_id="u-other-owner",
        type="note",
        title="其他人的遗赠",
        content="z",
        beneficiary_user_ids=["u-bene"],
        delivery_trigger=TRIGGER_MANUAL,
    )
    inherited = store.list_inherited("u-bene")
    assert len(inherited) == 3
    titles = [e["title"] for e in inherited]
    assert "继承1" in titles
    assert "继承2-on_date" in titles
    assert "其他人的遗赠" in titles
    # on_date 未来时间的应为 pending
    for e in inherited:
        if e["title"] == "继承2-on_date":
            assert e["status"] == "pending"
        if e["title"] == "继承1":
            assert e["status"] == "deliverable"


# =====================================================================
# 10. 删除成功
# =====================================================================
def test_delete_item(tmp_path: Path):
    store = _make_store(tmp_path)
    item = store.add_item(
        owner_user_id="u-owner",
        type="note",
        title="待删",
        content="x",
        beneficiary_user_ids=["u-bene"],
    )
    # 删除前能拿到
    assert store.get_item(item.item_id, "u-owner") is not None
    # 仅 owner 能删
    assert store.delete_item(item.item_id, "u-owner") is True
    # 删除后拿不到
    assert store.get_item(item.item_id, "u-owner") is None
    # 重复删除返回 False
    assert store.delete_item(item.item_id, "u-owner") is False
    # 加密文件应已删除
    assert not store._item_file("u-owner", item.item_id).exists()
