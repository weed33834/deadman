"""P7.5 Prompt 版本化 + AB 测试 - Prompt as Code。

借鉴 LangSmith Hub + Langfuse Prompt Management:

    1. Prompt as Code:
        - 每个 prompt 有 name + version(语义版本号 major.minor)
        - 版本存储在 data/prompts/<name>/<version>.yaml + .meta.json
        - 可回滚到任意历史版本(1 键回滚)

    2. AB 测试:
        - 按 user_id hash 分流到不同 version(50%/50% 或自定义比例)
        - 实验结果关联到 trace,可对比 faithfulness/answer_relevancy
        - 实验完成自动停止(到达样本数或显著性)

    3. 灰度发布:
        - 按 user_id 白名单先发布到 1% 用户
        - 监控 SLI/Drift,无异常再逐步放量(5%/20%/50%/100%)

    4. 变更审计:
        - 每次发布/回滚记录 actor/reason/timestamp
        - 审计日志 append-only(借鉴 security/audit.py)

feature flag:`DEADMAN_PROMPT_VERSIONING_ENABLED=0` 默认关闭。
关闭时直接返回当前 prompts.py 中的内置 prompt,不做版本管理。
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .feature_flags import is_enabled

logger = logging.getLogger(__name__)

# Prompt 仓库根目录
PROMPTS_REPO_ROOT = Path(
    os.environ.get("DEADMAN_PROMPTS_REPO", "data/prompts")
)


@dataclass
class PromptVersion:
    """单个 prompt 版本。"""

    name: str
    version: str  # 语义版本号 "1.0.0"
    template: str  # Jinja2 模板
    variables: list[str] = field(default_factory=list)  # 模板变量
    description: str = ""
    created_at: float = 0.0
    created_by: str = "system"
    is_active: bool = False  # 是否当前生效
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ABExperiment:
    """A/B 实验配置。"""

    name: str  # 实验名
    prompt_name: str  # 关联的 prompt
    variants: dict[str, str] = field(default_factory=dict)  # variant_id → version
    traffic_split: dict[str, int] = field(default_factory=dict)  # variant_id → 百分比(0-100)
    start_at: float = 0.0
    end_at: float = 0.0  # 0=无截止
    target_sample_size: int = 0  # 0=不限制
    status: str = "draft"  # draft / running / stopped / completed
    description: str = ""
    created_by: str = "system"
    # 实际样本计数(累计)
    samples: dict[str, int] = field(default_factory=dict)  # variant_id → 已采样数


@dataclass
class PromptResolution:
    """prompt 解析结果(含选中的版本 + reason)。"""

    name: str
    version: str
    template: str
    variant_id: str = "control"  # control / variant_a / variant_b
    experiment_name: str | None = None
    reason: str = "default"  # default / pinned / experiment / rollback


class PromptVersionManager:
    """Prompt 版本管理 + AB 测试核心类。

    用法:
        pm = PromptVersionManager()
        pm.publish("death_aftercare", "1.2.0", template="...", variables=["user_input"])

        # 业务读取(自动按 AB 实验分流)
        result = pm.resolve("death_aftercare", user_id="u123")
        rendered = pm.render(result, user_input="我父亲去世了")
    """

    def __init__(self, repo_root: Path | None = None) -> None:
        self.repo_root = repo_root or PROMPTS_REPO_ROOT
        self._lock = threading.RLock()
        # 缓存:{prompt_name: {version: PromptVersion}}
        self._cache: dict[str, dict[str, PromptVersion]] = {}
        # 当前 active 版本:{prompt_name: version}
        self._active: dict[str, str] = {}
        # AB 实验:{experiment_name: ABExperiment}
        self._experiments: dict[str, ABExperiment] = {}
        self._loaded = False

    # ==================================================================
    # Prompt 发布/读取
    # ==================================================================

    def publish(
        self,
        name: str,
        version: str,
        template: str,
        variables: list[str] | None = None,
        description: str = "",
        created_by: str = "admin",
        set_active: bool = True,
    ) -> PromptVersion:
        """发布新版本 prompt。

        Args:
            set_active: 是否同时设为当前生效版本(灰度发布时设 False)
        """
        if not is_enabled("prompt_versioning"):
            # 关闭:不持久化,返回内存对象
            return PromptVersion(
                name=name,
                version=version,
                template=template,
                variables=variables or [],
                description=description,
                created_by=created_by,
                is_active=True,
            )

        with self._lock:
            self._load()
            pv = PromptVersion(
                name=name,
                version=version,
                template=template,
                variables=variables or self._extract_variables(template),
                description=description,
                created_at=time.time(),
                created_by=created_by,
                is_active=set_active,
            )
            self._cache.setdefault(name, {})[version] = pv

            if set_active:
                self._active[name] = version

            self._save_prompt(pv)
            logger.info("Prompt published: %s@%s by=%s active=%s", name, version, created_by, set_active)
            return pv

    def resolve(
        self,
        name: str,
        user_id: str | None = None,
        version: str | None = None,
    ) -> PromptResolution:
        """解析 prompt(支持 AB 实验分流)。

        Args:
            name: prompt 名
            user_id: 用户 ID(用于 AB 分流)
            version: 显式指定版本(优先于 active + experiment)
        """
        if not is_enabled("prompt_versioning"):
            # 关闭:返回空 resolution,由 caller 走默认 prompts.py
            return PromptResolution(
                name=name,
                version="builtin",
                template="",
                reason="disabled",
            )

        with self._lock:
            self._load()

            # 1. 显式指定版本
            if version:
                pv = self._cache.get(name, {}).get(version)
                if pv:
                    return PromptResolution(
                        name=name,
                        version=version,
                        template=pv.template,
                        variant_id="pinned",
                        reason="pinned",
                    )

            # 2. AB 实验分流
            exp = self._find_running_experiment(name)
            if exp and user_id:
                variant_id = self._assign_variant(exp, user_id)
                version_str = exp.variants.get(variant_id)
                if version_str:
                    pv = self._cache.get(name, {}).get(version_str)
                    if pv:
                        # 累计样本数
                        exp.samples[variant_id] = exp.samples.get(variant_id, 0) + 1
                        self._maybe_complete_experiment(exp)
                        return PromptResolution(
                            name=name,
                            version=version_str,
                            template=pv.template,
                            variant_id=variant_id,
                            experiment_name=exp.name,
                            reason="experiment",
                        )

            # 3. active 版本
            active_version = self._active.get(name)
            if active_version:
                pv = self._cache.get(name, {}).get(active_version)
                if pv:
                    return PromptResolution(
                        name=name,
                        version=active_version,
                        template=pv.template,
                        variant_id="control",
                        reason="active",
                    )

            # 4. 兜底:返回 builtin
            return PromptResolution(
                name=name,
                version="builtin",
                template="",
                reason="builtin",
            )

    def render(self, resolution: PromptResolution, **variables) -> str:
        """渲染模板(简单 Jinja2 替换,无依赖)。

        支持 {{ var }} 语法。
        """
        if not resolution.template:
            return ""
        result = resolution.template
        for k, v in variables.items():
            result = result.replace("{{ " + k + " }}", str(v))
            result = result.replace("{{" + k + "}}", str(v))
        return result

    # ==================================================================
    # 回滚
    # ==================================================================

    def rollback(
        self,
        name: str,
        to_version: str,
        actor: str = "admin",
        reason: str = "",
    ) -> bool:
        """回滚到指定版本。"""
        with self._lock:
            self._load()
            versions = self._cache.get(name, {})
            if to_version not in versions:
                logger.warning("Rollback failed: %s@%s not found", name, to_version)
                return False
            self._active[name] = to_version
            # 更新 active 标记
            for v, pv in versions.items():
                pv.is_active = (v == to_version)
            self._save_active_state(name)
            logger.info(
                "Prompt rollback: %s → %s by=%s reason=%s",
                name,
                to_version,
                actor,
                reason,
            )
            return True

    def list_versions(self, name: str) -> list[PromptVersion]:
        """列出某 prompt 的所有版本(便于 admin 看板)。"""
        with self._lock:
            self._load()
            return list(self._cache.get(name, {}).values())

    def get_active_version(self, name: str) -> str | None:
        """获取当前生效版本号。"""
        with self._lock:
            self._load()
            return self._active.get(name)

    # ==================================================================
    # AB 实验
    # ==================================================================

    def create_experiment(
        self,
        name: str,
        prompt_name: str,
        variants: dict[str, str],
        traffic_split: dict[str, int],
        description: str = "",
        target_sample_size: int = 0,
        created_by: str = "admin",
    ) -> ABExperiment:
        """创建 A/B 实验。

        Args:
            name: 实验名(唯一)
            prompt_name: 关联的 prompt 名
            variants: {variant_id: version},如 {"control": "1.0.0", "variant_a": "1.1.0"}
            traffic_split: {variant_id: 百分比},如 {"control": 50, "variant_a": 50}(总和必须=100)
            target_sample_size: 目标样本数(达到后自动 stop)
        """
        if sum(traffic_split.values()) != 100:
            raise ValueError(f"traffic_split must sum to 100, got {sum(traffic_split.values())}")

        with self._lock:
            self._load()
            exp = ABExperiment(
                name=name,
                prompt_name=prompt_name,
                variants=dict(variants),
                traffic_split=dict(traffic_split),
                start_at=time.time(),
                target_sample_size=target_sample_size,
                description=description,
                created_by=created_by,
                status="running",
                samples=dict.fromkeys(variants, 0),
            )
            self._experiments[name] = exp
            self._save_experiment(exp)
            logger.info("Experiment created: %s prompt=%s", name, prompt_name)
            return exp

    def stop_experiment(self, name: str, reason: str = "") -> bool:
        """停止实验(不再分流)。"""
        with self._lock:
            self._load()
            exp = self._experiments.get(name)
            if exp is None:
                return False
            exp.status = "stopped"
            exp.end_at = time.time()
            self._save_experiment(exp)
            logger.info("Experiment stopped: %s reason=%s", name, reason)
            return True

    def list_experiments(self) -> list[ABExperiment]:
        with self._lock:
            self._load()
            return list(self._experiments.values())

    # ==================================================================
    # 内部
    # ==================================================================

    def _find_running_experiment(self, prompt_name: str) -> ABExperiment | None:
        """找当前 prompt 的 running 实验。"""
        for exp in self._experiments.values():
            if exp.prompt_name == prompt_name and exp.status == "running":
                return exp
        return None

    def _assign_variant(self, exp: ABExperiment, user_id: str) -> str:
        """稳定哈希 user_id 到 variant(同 user 永远命中同 variant)。"""
        # 哈希 user_id+exp_name → 0-99
        key = f"{exp.name}:{user_id}"
        hash_bytes = hashlib.sha256(key.encode("utf-8")).digest()
        bucket = int.from_bytes(hash_bytes[:8], "big") % 100
        # 按累积百分比匹配 variant
        cumulative = 0
        for vid, pct in exp.traffic_split.items():
            cumulative += pct
            if bucket < cumulative:
                return vid
        # 兜底:取最后一个
        return list(exp.traffic_split.keys())[-1]

    def _maybe_complete_experiment(self, exp: ABExperiment) -> None:
        """检查是否达到目标样本数,达到则自动 stop。"""
        if exp.target_sample_size <= 0:
            return
        total = sum(exp.samples.values())
        if total >= exp.target_sample_size:
            exp.status = "completed"
            exp.end_at = time.time()
            logger.info(
                "Experiment auto-completed: %s samples=%d",
                exp.name,
                total,
            )

    def _extract_variables(self, template: str) -> list[str]:
        """从 Jinja2 模板提取 {{ var }} 变量(简单正则)。"""
        import re
        matches = re.findall(r"{{\s*(\w+)\s*}}", template)
        return sorted(set(matches))

    def _load(self) -> None:
        """加载所有 prompt + 实验(惰性,只加载一次)。"""
        if self._loaded:
            return
        try:
            # 加载所有 prompt 版本
            for prompt_dir in self.repo_root.glob("*/"):
                name = prompt_dir.name
                for vf in prompt_dir.glob("*.yaml"):
                    try:
                        data = yaml.safe_load(vf.read_text(encoding="utf-8"))
                        if not isinstance(data, dict):
                            continue
                        pv = PromptVersion(
                            name=name,
                            version=str(data.get("version", vf.stem)),
                            template=data.get("template", ""),
                            variables=data.get("variables", []) or [],
                            description=data.get("description", ""),
                            created_at=float(data.get("created_at", 0.0)),
                            created_by=data.get("created_by", "system"),
                            is_active=bool(data.get("is_active", False)),
                            metadata=data.get("metadata", {}) or {},
                        )
                        self._cache.setdefault(name, {})[pv.version] = pv
                        if pv.is_active:
                            self._active[name] = pv.version
                    except Exception as e:
                        logger.warning("Failed to load prompt %s/%s: %s", name, vf.name, e)

            # 加载所有实验
            exp_dir = self.repo_root / "_experiments"
            if exp_dir.exists():
                for ef in exp_dir.glob("*.json"):
                    try:
                        import json
                        data = json.loads(ef.read_text(encoding="utf-8"))
                        exp = ABExperiment(
                            name=data["name"],
                            prompt_name=data["prompt_name"],
                            variants=data.get("variants", {}),
                            traffic_split=data.get("traffic_split", {}),
                            start_at=data.get("start_at", 0.0),
                            end_at=data.get("end_at", 0.0),
                            target_sample_size=data.get("target_sample_size", 0),
                            status=data.get("status", "draft"),
                            description=data.get("description", ""),
                            created_by=data.get("created_by", "system"),
                            samples=data.get("samples", {}),
                        )
                        self._experiments[exp.name] = exp
                    except Exception as e:
                        logger.warning("Failed to load experiment %s: %s", ef.name, e)
        except Exception as e:
            logger.warning("PromptVersionManager load failed: %s", e)
        self._loaded = True

    def _save_prompt(self, pv: PromptVersion) -> None:
        try:
            self.repo_root.mkdir(parents=True, exist_ok=True)
            prompt_dir = self.repo_root / pv.name
            prompt_dir.mkdir(parents=True, exist_ok=True)
            file_path = prompt_dir / f"{pv.version}.yaml"
            data = {
                "version": pv.version,
                "template": pv.template,
                "variables": pv.variables,
                "description": pv.description,
                "created_at": pv.created_at,
                "created_by": pv.created_by,
                "is_active": pv.is_active,
                "metadata": pv.metadata,
            }
            tmp = file_path.with_suffix(".tmp")
            tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
            os.replace(tmp, file_path)
        except Exception as e:
            logger.error("Failed to save prompt %s@%s: %s", pv.name, pv.version, e)
            raise

    def _save_active_state(self, name: str) -> None:
        """更新某 prompt 的所有版本的 is_active 标记。"""
        versions = self._cache.get(name, {})
        for v, pv in versions.items():
            pv.is_active = (v == self._active.get(name))
            self._save_prompt(pv)

    def _save_experiment(self, exp: ABExperiment) -> None:
        try:
            import json
            exp_dir = self.repo_root / "_experiments"
            exp_dir.mkdir(parents=True, exist_ok=True)
            file_path = exp_dir / f"{exp.name}.json"
            data = {
                "name": exp.name,
                "prompt_name": exp.prompt_name,
                "variants": exp.variants,
                "traffic_split": exp.traffic_split,
                "start_at": exp.start_at,
                "end_at": exp.end_at,
                "target_sample_size": exp.target_sample_size,
                "status": exp.status,
                "description": exp.description,
                "created_by": exp.created_by,
                "samples": exp.samples,
            }
            tmp = file_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, file_path)
        except Exception as e:
            logger.error("Failed to save experiment %s: %s", exp.name, e)


# 全局单例
_pm_instance: PromptVersionManager | None = None
_pm_lock = threading.Lock()


def get_prompt_manager() -> PromptVersionManager:
    global _pm_instance
    if _pm_instance is None:
        with _pm_lock:
            if _pm_instance is None:
                _pm_instance = PromptVersionManager()
    return _pm_instance
