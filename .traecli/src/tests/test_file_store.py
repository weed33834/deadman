"""测试 deadman.memory.file_store - 文件持久化记忆层

覆盖点（5 个）：
  - test_save_load_profile_roundtrip: save_profile + load_profile 往返一致性
  - test_pii_masking: PII 字段在落盘文件中被掩码（compliance-framework 数据安全）
  - test_append_episode: append_episode + load_episodes 追加与读取
  - test_atomic_write: _atomic_write 不残留 .tmp 文件
  - test_missing_file_returns_empty: 文件不存在时返回空结构，不抛异常

测试隔离：每个测试用 tmp_path fixture 独立目录，互不污染。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from deadman.memory.file_store import (
    FileMemoryStore,
    _atomic_write,
)
from deadman.memory.semantic import UserProfile


# =====================================================================
# 1. save_profile + load_profile 往返一致性
# =====================================================================


class TestSaveLoadProfileRoundtrip:
    """测试 save_profile + load_profile 往返一致性"""

    def test_save_load_profile_roundtrip(self, tmp_path: Path):
        # 构造带全部字段的 UserProfile
        # 注意：name 是 PII 字段，落盘时会被 sanitize_before_store 递归掩码
        # （deceased_info.name、family_structure 等嵌套 dict 中的 name 键也会被掩码）
        # 因此这里 deceased_info 不用 name 键，避免与 PII 脱敏逻辑耦合
        profile = UserProfile(
            user_id="user-001",
            name="张三",  # PII 字段，落盘后会被掩码
            relationship_to_deceased="子女",
            location={"country": "CN", "city": "北京"},
            deceased_info={"deceased_name": "张父", "death_date": "2024-01-15"},
            family_structure={"spouse": "李四"},
            assets_summary={"has_will": True},
            current_stage=2,
            completed_stages=[1],
            pending_tasks=["办理死亡证明", "联系殡仪馆"],
        )

        store = FileMemoryStore(memory_dir=tmp_path)
        store.save_profile("user-001", profile)

        # 文件应已生成
        assert store.user_file.exists(), "USER.md 应已生成"

        # 读回应保持字段一致（注意：顶层 name 字段经 PII 脱敏，不复等）
        loaded = store.load_profile("user-001")
        assert loaded is not None, "load_profile 不应返回 None"
        assert loaded.user_id == "user-001"
        assert loaded.relationship_to_deceased == "子女"
        assert loaded.location == {"country": "CN", "city": "北京"}
        # deceased_info 不含 PII 字段（用 deceased_name 而非 name），应原样保留
        assert loaded.deceased_info == {"deceased_name": "张父", "death_date": "2024-01-15"}
        assert loaded.family_structure == {"spouse": "李四"}
        assert loaded.assets_summary == {"has_will": True}
        assert loaded.current_stage == 2
        assert loaded.completed_stages == [1]
        assert loaded.pending_tasks == ["办理死亡证明", "联系殡仪馆"]

    def test_load_profile_user_id_mismatch_returns_none(self, tmp_path: Path):
        # user_id 不匹配应返回 None
        profile = UserProfile(user_id="user-A", name="A")
        store = FileMemoryStore(memory_dir=tmp_path)
        store.save_profile("user-A", profile)

        loaded = store.load_profile("user-B")
        assert loaded is None, "user_id 不匹配应返回 None"


# =====================================================================
# 2. PII 脱敏 - name 字段在落盘文件中被掩码
# =====================================================================


class TestPIIMasking:
    """测试 PII 字段脱敏 - compliance-framework 数据安全底线"""

    def test_pii_masking(self, tmp_path: Path):
        # name 是 PII 字段，落盘时应被掩码
        profile = UserProfile(
            user_id="user-pii",
            name="王小明",  # PII 字段，应被脱敏
            relationship_to_deceased="子女",
            location={"city": "上海"},
        )
        store = FileMemoryStore(memory_dir=tmp_path)
        store.save_profile("user-pii", profile)

        # 直接读文件原始内容，明文 name 不应出现
        raw = store.user_file.read_text(encoding="utf-8")
        assert "王小明" not in raw, (
            "PII 字段 name 不应以明文出现在 USER.md（应被 sanitize_before_store 掩码）"
        )
        # 掩码标记 *** 应出现
        assert "***" in raw, "脱敏后的掩码标记 *** 应出现在文件中"

        # load_profile 读回的 name 应是脱敏值，而非原始明文
        loaded = store.load_profile("user-pii")
        assert loaded is not None
        assert loaded.name != "王小明", "读回的 name 应为脱敏值"
        assert "***" in (loaded.name or ""), "读回的 name 应包含掩码标记"


# =====================================================================
# 3. append_episode + load_episodes 追加与读取
# =====================================================================


class TestAppendEpisode:
    """测试 append_episode + load_episodes"""

    def test_append_episode(self, tmp_path: Path):
        store = FileMemoryStore(memory_dir=tmp_path)

        # 追加 3 条 episode
        ts1 = datetime(2024, 1, 1, 10, 0)
        ts2 = datetime(2024, 1, 2, 11, 30)
        ts3 = datetime(2024, 1, 3, 14, 15)
        store.append_episode("sess-1", "用户咨询户籍注销", ts1)
        store.append_episode("sess-1", "用户询问殡仪馆联系方式", ts2)
        store.append_episode("sess-2", "用户询问遗产继承", ts3)

        # 文件应已生成
        assert store.episodes_file.exists(), "EPISODES.md 应已生成"

        # load_episodes 默认 limit=20，应返回 3 条
        episodes = store.load_episodes()
        assert len(episodes) == 3, "应返回 3 条 episode"

        # 校验第一条字段解析正确
        first = episodes[0]
        assert first["session"] == "sess-1"
        assert first["summary"] == "用户咨询户籍注销"
        assert first["timestamp"] == ts1, "时间戳应解析回 datetime"

        # 校验 limit=2 取最近 2 条
        recent = store.load_episodes(limit=2)
        assert len(recent) == 2
        assert recent[-1]["summary"] == "用户询问遗产继承"
        assert recent[0]["summary"] == "用户询问殡仪馆联系方式"

    def test_append_episode_multiline_summary_flattened(self, tmp_path: Path):
        # summary 含换行应被强制单行化，不破坏 EPISODES.md 行格式
        store = FileMemoryStore(memory_dir=tmp_path)
        store.append_episode(
            "sess-x", "第一行\n第二行\n第三行", datetime(2024, 1, 1, 10, 0)
        )

        episodes = store.load_episodes()
        assert len(episodes) == 1
        # 换行应被替换为空格
        assert "\n" not in episodes[0]["summary"]
        assert "第一行" in episodes[0]["summary"]
        assert "第二行" in episodes[0]["summary"]


# =====================================================================
# 4. _atomic_write 不残留 .tmp 文件
# =====================================================================


class TestAtomicWrite:
    """测试 _atomic_write 原子写入"""

    def test_atomic_write(self, tmp_path: Path):
        target = tmp_path / "subdir" / "target.md"
        content = "# 测试内容\n原子写入\n"

        # 子目录不存在，_atomic_write 应自动创建
        _atomic_write(target, content)

        # 目标文件存在且内容正确
        assert target.exists(), "目标文件应存在"
        assert target.read_text(encoding="utf-8") == content

        # 不应残留 .tmp 文件
        tmp_file = target.with_suffix(target.suffix + ".tmp")
        assert not tmp_file.exists(), "不应残留 .tmp 文件"

    def test_atomic_write_overwrite(self, tmp_path: Path):
        # 二次写入应覆盖原内容，且仍不残留 .tmp
        target = tmp_path / "target.md"
        _atomic_write(target, "第一版")
        _atomic_write(target, "第二版")

        assert target.read_text(encoding="utf-8") == "第二版"
        tmp_file = target.with_suffix(target.suffix + ".tmp")
        assert not tmp_file.exists()


# =====================================================================
# 5. 文件不存在时返回空结构，不抛异常
# =====================================================================


class TestMissingFileReturnsEmpty:
    """测试文件不存在时的兜底行为（韧性优先，不抛异常）"""

    def test_missing_file_returns_empty(self, tmp_path: Path):
        # 用一个全新的空目录，三个文件都不存在
        store = FileMemoryStore(memory_dir=tmp_path)

        # load_profile 应返回 None
        assert store.load_profile("any-user") is None

        # load_episodes 应返回空 list
        episodes = store.load_episodes()
        assert episodes == []

        # load_facts 应返回空 dict
        facts = store.load_facts()
        assert facts == {}

        # export_markdown 应返回非空字符串（含占位说明），不抛异常
        md = store.export_markdown()
        assert isinstance(md, str)
        assert "USER.md" in md  # 至少应含章节标题
        assert "MEMORY.md" in md
        assert "EPISODES.md" in md
        # 应含"未保存"占位提示
        assert "未保存" in md or "无" in md
