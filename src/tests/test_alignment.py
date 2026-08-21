"""P8.7 模型微调与私有化框架测试。

覆盖:
    - DPOTrainer: add preference + train + save/load checkpoint + evaluate + trust filter
    - SFTDataset: add + filter_by_quality + filter_by_task_type + dedup + balance
                  + export formats + validate + PII redaction
    - LocalLLMClient: config + chat + health_check + load/unload + stats + mock fallback
    - MoERouter: register + route + update_load + record_result + capacity protection
    - ContinuousLearner: record_feedback + extract preference pair + weekly review
                         + auto promote + forget user + reflexion integration
    - AlignmentManager end-to-end
    - Disabled state raises AlignmentDisabledError
"""

from __future__ import annotations

import json
import threading

import pytest


# =====================================================================
# 公共 fixture
# =====================================================================
@pytest.fixture(autouse=True)
def enable_alignment(monkeypatch, tmp_path):
    """每个测试启用 alignment + defense feature flag。

    并将 tenant 数据目录重定向到 tmp_path(避免污染 ~/.deadman)。
    """
    monkeypatch.setenv("DEADMAN_ALIGNMENT_ENABLED", "1")
    monkeypatch.setenv("DEADMAN_DEFENSE_ENABLED", "1")
    monkeypatch.setenv("DEADMAN_FEATURE_FLAG_SYSTEM_ENABLED", "1")
    # 把 tenant 根目录重定向到 tmp(避免污染用户 home)
    monkeypatch.setenv("DEADMAN_TENANTS_ROOT", str(tmp_path / "tenants"))
    monkeypatch.setenv("DEADMAN_DEFAULT_TENANT_ID", "test-tenant")

    # 清 flag 缓存,确保新 env 生效
    from deadman.infrastructure.feature_flags import get_flags

    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0

    # 重置 AlignmentManager 单例
    from deadman.alignment.manager import reset_alignment_manager

    reset_alignment_manager()

    yield

    # 测试后清理
    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    reset_alignment_manager()


# =====================================================================
# DPOTrainer
# =====================================================================
class TestDPOTrainer:
    def test_add_preference_and_count(self):
        from deadman.alignment.dpo_trainer import DPOTrainer, PreferenceExample

        trainer = DPOTrainer()
        ex = PreferenceExample(
            prompt="如何立遗嘱?",
            chosen_response="建议咨询专业律师...",
            rejected_response="我不知道。",
        )
        assert trainer.add_preference(ex) is True
        assert trainer.preference_count() == 1

    def test_add_preference_pii_redaction(self):
        """包含 PII 的偏好对应被脱敏(prompt/chosen/rejected 三段)。"""
        from deadman.alignment.dpo_trainer import DPOTrainer, PreferenceExample

        trainer = DPOTrainer()
        ex = PreferenceExample(
            prompt="我的手机号是 13812345678,如何联系律师?",
            chosen_response="可以拨打 13812345678 联系张律师。",
            rejected_response="不知道。",
            user_id="u1",
        )
        trainer.add_preference(ex)
        stored = trainer.preferences()[0]
        assert stored.redacted is True
        # PII 应被脱敏(原号不出现)
        assert "13812345678" not in stored.prompt
        assert "13812345678" not in stored.chosen_response

    def test_train_mock_metrics(self):
        from deadman.alignment.dpo_trainer import (
            DPOConfig,
            DPOTrainer,
            PreferenceExample,
        )

        trainer = DPOTrainer()
        for i in range(5):
            trainer.add_preference(
                PreferenceExample(
                    prompt=f"prompt-{i}",
                    chosen_response=f"good answer {i} " * 10,
                    rejected_response=f"bad answer {i}",
                    user_id="u1",
                )
            )
        config = DPOConfig(max_steps=20, save_steps=5, min_trust_score=0.0)
        report = trainer.train(config)
        assert report.completed is True
        assert report.total_steps == 20
        assert report.samples_used == 5
        assert report.final_loss > 0
        # loss 应该比初始值(2.2)小
        assert report.final_loss < 2.2
        assert 0.5 <= report.final_reward_accuracy <= 1.0
        assert report.checkpoints_saved == 4  # 20 / 5

    def test_train_no_samples_returns_error(self):
        from deadman.alignment.dpo_trainer import DPOConfig, DPOTrainer

        trainer = DPOTrainer()
        report = trainer.train(DPOConfig(max_steps=10))
        assert report.completed is False
        assert "no samples" in report.error

    def test_save_and_load_checkpoint(self, tmp_path):
        from deadman.alignment.dpo_trainer import (
            DPOConfig,
            DPOTrainer,
            PreferenceExample,
        )

        trainer = DPOTrainer()
        for i in range(3):
            trainer.add_preference(
                PreferenceExample(
                    prompt=f"prompt-{i}",
                    chosen_response=f"chosen-{i}",
                    rejected_response=f"rejected-{i}",
                    user_id="u1",
                )
            )
        trainer.train(DPOConfig(max_steps=5, save_steps=5, min_trust_score=0.0))

        ckpt = tmp_path / "dpo_checkpoint.json"
        trainer.save_checkpoint(ckpt)
        assert ckpt.exists()

        # 新 trainer 加载
        trainer2 = DPOTrainer()
        ok = trainer2.load_checkpoint(ckpt)
        assert ok is True
        assert trainer2.preference_count() == 3
        # 加载后样本应保留
        prefs = trainer2.preferences()
        assert prefs[0].prompt == "prompt-0"

    def test_evaluate_returns_metrics(self):
        from deadman.alignment.dpo_trainer import DPOTrainer, PreferenceExample

        trainer = DPOTrainer()
        eval_set = [
            PreferenceExample(
                prompt="q1",
                chosen_response="very good detailed answer " * 5,
                rejected_response="bad",
                trust_score=0.9,
            ),
            PreferenceExample(
                prompt="q2",
                chosen_response="another good answer " * 3,
                rejected_response="no",
                trust_score=0.8,
            ),
        ]
        report = trainer.evaluate(eval_set)
        assert report.samples_evaluated == 2
        assert 0.0 <= report.accuracy <= 1.0
        assert "user_feedback" in report.per_source_accuracy

    def test_trust_score_filter_low_quality(self):
        """trust_score 低于 0.1 的样本被拒收。"""
        from deadman.alignment.dpo_trainer import DPOTrainer, PreferenceExample, PreferenceSource

        trainer = DPOTrainer()
        # SYNTHETIC + 无 user_id → trust = 0.4 * 0.2 = 0.08 < 0.1,拒收
        low = PreferenceExample(
            prompt="q",
            chosen_response="c",
            rejected_response="r",
            source=PreferenceSource.SYNTHETIC,
            user_id="",
        )
        # 让 add 不重新计算 trust_score(传入非零初始值)
        low.trust_score = 0.05
        result = trainer.add_preference(low)
        assert result is False
        assert trainer.preference_count() == 0

    def test_load_nonexistent_checkpoint_returns_false(self, tmp_path):
        from deadman.alignment.dpo_trainer import DPOTrainer

        trainer = DPOTrainer()
        assert trainer.load_checkpoint(tmp_path / "nope.json") is False


# =====================================================================
# SFTDataset
# =====================================================================
class TestSFTDataset:
    def test_add_and_count(self):
        from deadman.alignment.sft_dataset import SFTDataset, SFTExample, TaskType

        ds = SFTDataset()
        ds.add(
            SFTExample(
                prompt="如何立遗嘱?",
                completion="立遗嘱需要...",
                task_type=TaskType.LEGAL,
                quality_score=0.9,
            )
        )
        assert ds.count() == 1

    def test_add_pii_redaction(self):
        """添加时强制 PII 脱敏(prompt + completion)。"""
        from deadman.alignment.sft_dataset import SFTDataset, SFTExample, TaskType

        ds = SFTDataset()
        ds.add(
            SFTExample(
                prompt="我的手机 13812345678 想咨询遗嘱",
                completion="请拨打 13812345678",
                task_type=TaskType.LEGAL,
            )
        )
        ex = ds.examples()[0]
        assert ex.redacted is True
        assert "13812345678" not in ex.prompt
        assert "13812345678" not in ex.completion

    def test_filter_by_quality(self):
        from deadman.alignment.sft_dataset import SFTDataset, SFTExample

        ds = SFTDataset()
        ds.add(SFTExample(prompt="q1", completion="c1", quality_score=0.3))
        ds.add(SFTExample(prompt="q2", completion="c2", quality_score=0.7))
        ds.add(SFTExample(prompt="q3", completion="c3", quality_score=0.9))
        filtered = ds.filter_by_quality(0.7)
        assert filtered.count() == 2
        # 原 dataset 不变
        assert ds.count() == 3

    def test_filter_by_task_type(self):
        from deadman.alignment.sft_dataset import SFTDataset, SFTExample, TaskType

        ds = SFTDataset()
        ds.add(SFTExample(prompt="q1", completion="c1", task_type=TaskType.LEGAL))
        ds.add(SFTExample(prompt="q2", completion="c2", task_type=TaskType.MEDICAL))
        ds.add(SFTExample(prompt="q3", completion="c3", task_type=TaskType.LEGAL))
        legal = ds.filter_by_task_type(TaskType.LEGAL)
        assert legal.count() == 2

    def test_deduplicate(self):
        from deadman.alignment.sft_dataset import SFTDataset, SFTExample

        ds = SFTDataset()
        ds.add(SFTExample(prompt="same prompt", completion="c1"))
        ds.add(SFTExample(prompt="same prompt", completion="c2"))
        ds.add(SFTExample(prompt="different", completion="c3"))
        removed = ds.deduplicate()
        assert removed == 1
        assert ds.count() == 2

    def test_balance_classes(self):
        from deadman.alignment.sft_dataset import SFTDataset, SFTExample, TaskType

        ds = SFTDataset()
        # 3 LEGAL + 1 MEDICAL → 均衡后各 1
        for i in range(3):
            ds.add(
                SFTExample(
                    prompt=f"legal-{i}",
                    completion=f"c-{i}",
                    task_type=TaskType.LEGAL,
                    quality_score=0.5 + i * 0.1,
                )
            )
        ds.add(
            SFTExample(
                prompt="med-0",
                completion="cm-0",
                task_type=TaskType.MEDICAL,
                quality_score=0.9,
            )
        )
        removed = ds.balance_classes()
        assert removed == 2  # 移除 2 个 LEGAL
        assert ds.count() == 2
        # 各类各 1 个
        task_counts = {}
        for ex in ds.examples():
            task_counts[ex.task_type] = task_counts.get(ex.task_type, 0) + 1
        assert task_counts.get(TaskType.LEGAL, 0) == 1
        assert task_counts.get(TaskType.MEDICAL, 0) == 1

    def test_export_jsonl(self):
        from deadman.alignment.sft_dataset import ExportFormat, SFTDataset, SFTExample

        ds = SFTDataset()
        ds.add(SFTExample(prompt="q1", completion="c1"))
        ds.add(SFTExample(prompt="q2", completion="c2"))
        data = ds.export(ExportFormat.JSONL)
        text = data.decode("utf-8")
        lines = [line for line in text.strip().split("\n") if line]
        assert len(lines) == 2
        parsed = json.loads(lines[0])
        assert "prompt" in parsed
        assert "completion" in parsed

    def test_export_csv(self):
        from deadman.alignment.sft_dataset import ExportFormat, SFTDataset, SFTExample

        ds = SFTDataset()
        ds.add(SFTExample(prompt="q1", completion="c1"))
        data = ds.export(ExportFormat.CSV)
        text = data.decode("utf-8")
        # CSV 表头
        assert "prompt" in text.split("\n")[0]
        assert "completion" in text.split("\n")[0]
        # 至少 2 行(表头 + 1 数据)
        assert len(text.strip().split("\n")) >= 2

    def test_export_alpaca_format(self):
        from deadman.alignment.sft_dataset import ExportFormat, SFTDataset, SFTExample

        ds = SFTDataset()
        ds.add(SFTExample(prompt="instruction\ntext", completion="output"))
        data = ds.export(ExportFormat.ALPACA)
        items = json.loads(data.decode("utf-8"))
        assert len(items) == 1
        assert "instruction" in items[0]
        assert "input" in items[0]
        assert "output" in items[0]

    def test_export_sharegpt_format(self):
        from deadman.alignment.sft_dataset import ExportFormat, SFTDataset, SFTExample

        ds = SFTDataset()
        ds.add(SFTExample(prompt="hi", completion="hello"))
        data = ds.export(ExportFormat.SHAREGPT)
        items = json.loads(data.decode("utf-8"))
        assert len(items) == 1
        convs = items[0]["conversations"]
        assert len(convs) == 2
        assert convs[0]["from"] == "human"
        assert convs[1]["from"] == "gpt"

    def test_validate_correct_dataset(self):
        from deadman.alignment.sft_dataset import SFTDataset, SFTExample, TaskType

        ds = SFTDataset()
        ds.add(
            SFTExample(
                prompt="q1",
                completion="c1",
                task_type=TaskType.LEGAL,
                quality_score=0.8,
            )
        )
        result = ds.validate()
        assert result["valid"] is True
        assert result["stats"]["total"] == 1
        assert result["errors"] == []

    def test_validate_detects_unredacted(self):
        """手动构造未脱敏样本 → validate 报错。"""
        from deadman.alignment.sft_dataset import SFTDataset, SFTExample

        ds = SFTDataset()
        # 直接 append,跳过 add 的脱敏
        ex = SFTExample(prompt="q", completion="c", redacted=False)
        ds._examples.append(ex)
        result = ds.validate()
        assert result["valid"] is False
        assert any("redacted" in e for e in result["errors"])

    def test_save_and_load_roundtrip(self, tmp_path):
        from deadman.alignment.sft_dataset import SFTDataset, SFTExample, TaskType

        ds = SFTDataset()
        ds.add(
            SFTExample(
                prompt="q1",
                completion="c1",
                task_type=TaskType.LEGAL,
                quality_score=0.9,
            )
        )
        # save 到 tenant 路径
        ds.save("alignment/test_sft.jsonl")
        # 新 dataset 加载
        ds2 = SFTDataset()
        loaded = ds2.load("alignment/test_sft.jsonl")
        assert loaded == 1
        assert ds2.count() == 1
        ex = ds2.examples()[0]
        assert ex.task_type.value == "legal"


# =====================================================================
# LocalLLMClient
# =====================================================================
class TestLocalLLMClient:
    def test_config_defaults(self):
        from deadman.alignment.local_llm import LocalLLMConfig, LocalLLMProvider

        config = LocalLLMConfig(
            provider=LocalLLMProvider.OLLAMA,
            model_path="llama3",
        )
        # port 默认填充
        assert config.port == 11434  # Ollama 默认端口
        # api_base 自动构造
        assert config.api_base.startswith("http://localhost:")
        assert "/v1" in config.chat_endpoint

    def test_chat_mock_mode(self):
        """mock_mode=True → chat 返回 mock 响应。"""
        from deadman.alignment.local_llm import (
            LocalLLMClient,
            LocalLLMConfig,
            LocalLLMProvider,
        )

        config = LocalLLMConfig(
            provider=LocalLLMProvider.OLLAMA,
            model_path="llama3",
            mock_mode=True,
        )
        client = LocalLLMClient(config)
        reply = client.chat(
            [
                {"role": "user", "content": "你好"},
            ]
        )
        assert "mock-ollama" in reply
        assert "你好" in reply

    def test_health_check_returns_false_in_mock(self):
        from deadman.alignment.local_llm import (
            LocalLLMClient,
            LocalLLMConfig,
            LocalLLMProvider,
        )

        config = LocalLLMConfig(
            provider=LocalLLMProvider.VLLM,
            mock_mode=True,
        )
        client = LocalLLMClient(config)
        assert client.health_check() is False

    def test_health_check_false_activates_mock(self):
        """health_check 失败 → mock_active = True,后续 chat 走 mock。"""
        from deadman.alignment.local_llm import (
            LocalLLMClient,
            LocalLLMConfig,
            LocalLLMProvider,
        )

        # 用一个不存在的端口,确保 health_check 失败
        config = LocalLLMConfig(
            provider=LocalLLMProvider.CUSTOM,
            port=59999,  # 大概率无服务
            timeout_seconds=0.5,
        )
        client = LocalLLMClient(config)
        ok = client.health_check()
        assert ok is False
        assert client.mock_active is True
        # 后续 chat 走 mock
        reply = client.chat([{"role": "user", "content": "hello"}])
        assert "mock" in reply

    def test_load_and_unload_model(self):
        from deadman.alignment.local_llm import (
            LocalLLMClient,
            LocalLLMConfig,
            LocalLLMProvider,
        )

        config = LocalLLMConfig(
            provider=LocalLLMProvider.VLLM,
            model_path="Qwen/Qwen2.5-7B-Instruct",
            gpu_required=True,
        )
        client = LocalLLMClient(config)
        assert client.is_loaded is False
        client.load_model()
        assert client.is_loaded is True
        # 7B 模型应该估算 ~14GB
        stats = client.get_stats()
        assert stats["gpu_memory_mb"] == 14_000
        assert stats["model_loaded"] is True
        client.unload_model()
        assert client.is_loaded is False
        assert client.get_stats()["gpu_memory_mb"] == 0

    def test_get_stats_tracking(self):
        from deadman.alignment.local_llm import (
            LocalLLMClient,
            LocalLLMConfig,
            LocalLLMProvider,
        )

        config = LocalLLMConfig(
            provider=LocalLLMProvider.OLLAMA,
            mock_mode=True,
        )
        client = LocalLLMClient(config)
        client.chat([{"role": "user", "content": "hello world"}])
        client.chat([{"role": "user", "content": "second call"}])
        stats = client.get_stats()
        assert stats["total_calls"] == 2
        assert stats["provider"] == "ollama"
        assert stats["mock_active"] is True

    def test_provider_enum_values(self):
        from deadman.alignment.local_llm import LocalLLMProvider

        assert LocalLLMProvider.QWEN.value == "qwen"
        assert LocalLLMProvider.DEEPSEEK.value == "deepseek"
        assert LocalLLMProvider.LLAMA.value == "llama"
        assert LocalLLMProvider.OLLAMA.value == "ollama"
        assert LocalLLMProvider.VLLM.value == "vllm"
        assert LocalLLMProvider.CUSTOM.value == "custom"


# =====================================================================
# MoERouter
# =====================================================================
class TestMoERouter:
    def test_register_and_list(self):
        from deadman.alignment.moe_router import (
            Expert,
            ExpertSpecialization,
            MoERouter,
        )

        router = MoERouter()
        assert (
            router.register_expert(
                Expert(
                    name="legal-1",
                    specialization=ExpertSpecialization.LEGAL,
                )
            )
            is True
        )
        assert len(router.list_experts()) == 1

    def test_route_selects_matching_specialization(self):
        from deadman.alignment.moe_router import (
            Expert,
            ExpertSpecialization,
            MoERouter,
        )

        router = MoERouter()
        router.register_expert(
            Expert(
                name="legal-1",
                specialization=ExpertSpecialization.LEGAL,
                capacity=10,
            )
        )
        router.register_expert(
            Expert(
                name="general-1",
                specialization=ExpertSpecialization.GENERAL,
                capacity=10,
            )
        )
        expert = router.route("如何立遗嘱?")
        assert expert.specialization == ExpertSpecialization.LEGAL

    def test_route_uses_context_task_type(self):
        from deadman.alignment.moe_router import (
            Expert,
            ExpertSpecialization,
            MoERouter,
        )

        router = MoERouter()
        router.register_expert(
            Expert(
                name="med-1",
                specialization=ExpertSpecialization.MEDICAL,
                capacity=10,
            )
        )
        router.register_expert(
            Expert(
                name="general-1",
                specialization=ExpertSpecialization.GENERAL,
                capacity=10,
            )
        )
        # context 显式指定 medical
        expert = router.route("hello", context={"task_type": "medical"})
        assert expert.specialization == ExpertSpecialization.MEDICAL

    def test_update_load(self):
        from deadman.alignment.moe_router import (
            Expert,
            ExpertSpecialization,
            MoERouter,
        )

        router = MoERouter()
        router.register_expert(
            Expert(
                name="e1",
                specialization=ExpertSpecialization.GENERAL,
                capacity=10,
            )
        )
        assert router.update_load("e1", +3) is True
        assert router.get_expert("e1").current_load == 3
        assert router.update_load("e1", -1) is True
        assert router.get_expert("e1").current_load == 2
        # 不存在
        assert router.update_load("nope", +1) is False

    def test_record_result_updates_success_rate(self):
        from deadman.alignment.moe_router import (
            Expert,
            ExpertSpecialization,
            MoERouter,
        )

        router = MoERouter()
        router.register_expert(
            Expert(
                name="e1",
                specialization=ExpertSpecialization.GENERAL,
                success_rate=1.0,
            )
        )
        # 默认 alpha=0.1,失败一次 → 0.9
        router.record_result("e1", success=False)
        assert router.get_expert("e1").success_rate == pytest.approx(0.9, abs=0.01)
        # 成功一次 → 0.9 * 0.9 + 0.1 * 1 = 0.91
        router.record_result("e1", success=True)
        assert router.get_expert("e1").success_rate == pytest.approx(0.91, abs=0.01)
        assert router.get_expert("e1").total_requests == 2

    def test_capacity_protection(self):
        """已达 capacity 的专家被跳过。"""
        from deadman.alignment.moe_router import (
            Expert,
            ExpertSpecialization,
            MoERouter,
        )

        router = MoERouter()
        router.register_expert(
            Expert(
                name="legal-1",
                specialization=ExpertSpecialization.LEGAL,
                capacity=2,
                current_load=2,  # 已满
            )
        )
        router.register_expert(
            Expert(
                name="general-1",
                specialization=ExpertSpecialization.GENERAL,
                capacity=10,
            )
        )
        # legal-1 已满,应路由到 general(回退策略)
        expert = router.route("如何立遗嘱?")
        assert expert.name == "general-1"

    def test_get_stats(self):
        from deadman.alignment.moe_router import (
            Expert,
            ExpertSpecialization,
            MoERouter,
        )

        router = MoERouter()
        router.register_expert(Expert(name="e1", specialization=ExpertSpecialization.LEGAL))
        stats = router.get_stats()
        assert stats["total_experts"] == 1
        assert len(stats["experts"]) == 1
        assert "load_ratio" in stats["experts"][0]


# =====================================================================
# ContinuousLearner
# =====================================================================
class TestContinuousLearner:
    def test_record_feedback(self):
        from deadman.alignment.continuous_learn import ContinuousLearner, FeedbackEvent

        learner = ContinuousLearner()
        event = FeedbackEvent(
            user_id="u1",
            query="q1",
            response="r1",
            rating=5,
        )
        learner.record_feedback(event)
        assert learner.event_count() == 1

    def test_record_feedback_pii_redaction(self):
        from deadman.alignment.continuous_learn import ContinuousLearner, FeedbackEvent

        learner = ContinuousLearner()
        event = FeedbackEvent(
            user_id="u1",
            query="我的手机 13812345678",
            response="请拨打 13812345678",
            rating=4,
        )
        learner.record_feedback(event)
        stored = learner.events()[0]
        assert "13812345678" not in stored.query
        assert "13812345678" not in stored.response

    def test_extract_preference_pair_chosen(self):
        """rating ≥ 4 → chosen pair。"""
        from deadman.alignment.continuous_learn import ContinuousLearner, FeedbackEvent
        from deadman.alignment.dpo_trainer import PreferenceSource

        learner = ContinuousLearner()
        event = FeedbackEvent(
            user_id="u1",
            query="q1",
            response="good answer",
            rating=5,
        )
        pair = learner.extract_preference_pair(event)
        assert pair is not None
        assert pair.chosen_response == "good answer"
        assert pair.source == PreferenceSource.USER_FEEDBACK

    def test_extract_preference_pair_rejected(self):
        """rating < 3 → rejected pair。"""
        from deadman.alignment.continuous_learn import ContinuousLearner, FeedbackEvent

        learner = ContinuousLearner()
        event = FeedbackEvent(
            user_id="u1",
            query="q1",
            response="bad answer",
            rating=1,
        )
        pair = learner.extract_preference_pair(event)
        assert pair is not None
        assert pair.rejected_response == "bad answer"

    def test_extract_preference_pair_neutral(self):
        """rating = 3 → 中性,返回 None。"""
        from deadman.alignment.continuous_learn import ContinuousLearner, FeedbackEvent

        learner = ContinuousLearner()
        event = FeedbackEvent(
            user_id="u1",
            query="q1",
            response="ok",
            rating=3,
        )
        pair = learner.extract_preference_pair(event)
        assert pair is None

    def test_weekly_review(self):
        from deadman.alignment.continuous_learn import ContinuousLearner, FeedbackEvent

        learner = ContinuousLearner()
        for r in [1, 2, 3, 4, 5]:
            learner.record_feedback(
                FeedbackEvent(
                    user_id=f"u{r}",
                    query=f"q{r}",
                    response=f"r{r}",
                    rating=r,
                    comment="差" if r <= 2 else "",
                )
            )
        report = learner.weekly_review(days=7)
        assert report.total_feedback == 5
        assert report.avg_rating == 3.0
        assert report.users_active == 5
        # rating=1,2 各 1 条带评论 → flagged=2
        assert report.flagged_for_review == 2
        assert report.rating_distribution[5] == 1

    def test_auto_promote_to_sft(self):
        from deadman.alignment.continuous_learn import ContinuousLearner, FeedbackEvent
        from deadman.alignment.sft_dataset import SFTDataset

        learner = ContinuousLearner()
        ds = SFTDataset()
        # rating=5 → quality = 1.0 + 0(无评论) = 1.0 ≥ 0.8 → 晋升
        learner.record_feedback(
            FeedbackEvent(
                user_id="u1",
                query="q1",
                response="r1",
                rating=5,
            )
        )
        # rating=3 → 不满足 min_rating=4 → 跳过
        learner.record_feedback(
            FeedbackEvent(
                user_id="u2",
                query="q2",
                response="r2",
                rating=3,
            )
        )
        promoted = learner.auto_promote_to_sft(ds, min_quality_score=0.8, min_rating=4)
        assert promoted == 1
        assert ds.count() == 1

    def test_forget_user(self):
        from deadman.alignment.continuous_learn import ContinuousLearner, FeedbackEvent

        learner = ContinuousLearner()
        learner.record_feedback(
            FeedbackEvent(
                user_id="u1",
                query="q1",
                response="r1",
                rating=5,
            )
        )
        learner.record_feedback(
            FeedbackEvent(
                user_id="u2",
                query="q2",
                response="r2",
                rating=4,
            )
        )
        removed = learner.forget_user("u1")
        assert removed == 1
        assert learner.event_count() == 1
        # 剩下 u2
        assert learner.events()[0].user_id == "u2"

    def test_feedback_event_rating_validation(self):
        from deadman.alignment.continuous_learn import FeedbackEvent

        with pytest.raises(ValueError):
            FeedbackEvent(user_id="u", query="q", response="r", rating=0)
        with pytest.raises(ValueError):
            FeedbackEvent(user_id="u", query="q", response="r", rating=6)


# =====================================================================
# AlignmentManager (end-to-end)
# =====================================================================
class TestAlignmentManager:
    def test_disabled_raises_error(self, monkeypatch):
        """DEADMAN_ALIGNMENT_ENABLED=0 → AlignmentDisabledError。"""
        # 关闭 flag
        monkeypatch.setenv("DEADMAN_ALIGNMENT_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags

        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0

        from deadman.alignment import AlignmentDisabledError, get_alignment_manager

        with pytest.raises(AlignmentDisabledError):
            get_alignment_manager()

    def test_init_raises_when_disabled(self, monkeypatch):
        monkeypatch.setenv("DEADMAN_ALIGNMENT_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags

        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0

        from deadman.alignment import AlignmentDisabledError, AlignmentManager

        with pytest.raises(AlignmentDisabledError):
            AlignmentManager()

    def test_get_manager_singleton(self):
        from deadman.alignment import AlignmentManager, get_alignment_manager

        m1 = get_alignment_manager()
        m2 = get_alignment_manager()
        assert m1 is m2
        assert isinstance(m1, AlignmentManager)

    def test_submit_feedback_end_to_end(self):
        from deadman.alignment import get_alignment_manager
        from deadman.alignment.continuous_learn import FeedbackEvent

        manager = get_alignment_manager()
        event = FeedbackEvent(
            user_id="u1",
            query="如何立遗嘱?",
            response="建议咨询律师...",
            rating=5,
        )
        result = manager.submit_feedback(event)
        assert result["recorded"] is True
        assert result["preference_added"] is True

    def test_run_training_pipeline(self):
        from deadman.alignment import get_alignment_manager
        from deadman.alignment.continuous_learn import FeedbackEvent

        manager = get_alignment_manager()
        # 收集多条反馈
        for i in range(5):
            manager.submit_feedback(
                FeedbackEvent(
                    user_id=f"u{i}",
                    query=f"q{i}",
                    response=f"good answer {i}" * 5,
                    rating=5,
                )
            )
        report = manager.run_training_pipeline()
        assert report.completed is True
        # 至少有一些 SFT 样本被晋升
        assert report.sft_samples > 0

    def test_route_query(self):
        from deadman.alignment import get_alignment_manager
        from deadman.alignment.moe_router import Expert

        manager = get_alignment_manager()
        model_name, expert = manager.route_query("如何立遗嘱?")
        assert isinstance(expert, Expert)
        assert isinstance(model_name, str)
        assert len(model_name) > 0

    def test_forget_user_end_to_end(self):
        from deadman.alignment import get_alignment_manager
        from deadman.alignment.continuous_learn import FeedbackEvent

        manager = get_alignment_manager()
        manager.submit_feedback(
            FeedbackEvent(
                user_id="u1",
                query="q1",
                response="r1",
                rating=5,
            )
        )
        result = manager.forget_user("u1")
        assert result["feedback_removed"] == 1

    def test_stats_aggregation(self):
        from deadman.alignment import get_alignment_manager

        manager = get_alignment_manager()
        stats = manager.stats()
        assert "dpo" in stats
        assert "sft" in stats
        assert "moe" in stats
        assert "continuous_learning" in stats

    def test_chat_without_llm_returns_mock(self):
        from deadman.alignment import get_alignment_manager

        manager = get_alignment_manager()
        # 未 attach local_llm → 返回 no-llm-attached 标记
        reply = manager.chat("hello")
        assert "no-llm-attached" in reply


# =====================================================================
# 并发安全测试
# =====================================================================
class TestConcurrency:
    def test_dpo_trainer_thread_safe(self):
        """多线程并发 add_preference 不丢数据。"""
        from deadman.alignment.dpo_trainer import DPOTrainer, PreferenceExample

        trainer = DPOTrainer()

        def add_n(start: int, n: int) -> None:
            for i in range(start, start + n):
                trainer.add_preference(
                    PreferenceExample(
                        prompt=f"q-{i}",
                        chosen_response=f"c-{i}",
                        rejected_response=f"r-{i}",
                        user_id="u1",
                    )
                )

        threads = [threading.Thread(target=add_n, args=(i * 100, 100)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert trainer.preference_count() == 400
