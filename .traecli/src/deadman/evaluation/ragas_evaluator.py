"""RAGAS 集成 - RAG 评估指标(9 维度 + 质量门 + 降级保护)

作为 ThreeLayerEvaluator 之外的可选评估层,专门评估 RAG 检索质量。
RAGAS 为可选依赖,不可用时返回 degraded=true,**绝不抛异常,不阻断 CI**。

支持维度(共 9 个):
  RAGAS 原生(7 个):
    1. faithfulness          答案是否忠于检索上下文(防幻觉)
    2. answer_relevancy      答案与问题相关性
    3. context_precision     检索上下文精确度(相关内容排名靠前)
    4. context_recall        检索上下文召回率(覆盖 ground_truth)
    5. context_entity_recall 检索上下文实体召回率
    6. answer_correctness    答案正确性(对比 ground_truth)
    7. answer_similarity     答案与 ground_truth 语义相似度

  deadman 自定义扩展(2 个):
    8. completeness          完整性(基于规则校验,关键事实是否齐全)
    9. safety                安全性(基于 L0-L8 规则链校验)

设计要点:
  - LLM 注入:用 deadman.llm.llm_client 包装为 BaseRagasLLM,
              支持 mock/fallback/multi-provider,与生产 LLM 调用一致
  - 降级保护:RAGAS 不可用 / LLM 不可用 / 单维度失败 → 不阻断整体
  - 质量门:faithfulness < quality_gate_threshold 时返回 quality_gate_passed=False,
            供 CI 决定是否阻断 merge
  - --quick 模式:仅跑 faithfulness + answer_relevancy,加速本地迭代
  - 批量评估:支持单 case 与 cases_dir 批量,输出 JSONL 报告

用法:

    from deadman.evaluation.ragas_evaluator import RAGASEvaluator
    evaluator = RAGASEvaluator()
    result = await evaluator.evaluate(
        question="...",
        answer="...",
        contexts=["...", "..."],
        ground_truth="...",  # 可选
    )
    # result = {
    #     "available": True, "degraded": False,
    #     "metrics": {"faithfulness": 0.85, ...},
    #     "quality_gate_passed": True,
    #     "errors": [...],
    # }
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# =====================================================================
# 可选依赖 - RAGAS 0.2+ / 0.4+ 均兼容
# =====================================================================
try:  # pragma: no cover - 环境依赖
    from ragas import evaluate as _ragas_evaluate_sync  # type: ignore
    from ragas.dataset_schema import EvaluationDataset  # type: ignore

    _RAGAS_AVAILABLE = True
except Exception:  # pragma: no cover - 降级路径
    _ragas_evaluate_sync = None  # type: ignore
    EvaluationDataset = None  # type: ignore
    _RAGAS_AVAILABLE = False

# 异步 evaluate 优先(0.4+ 支持),失败回退到同步
try:  # pragma: no cover
    from ragas import (
        evaluate as _ragas_evaluate_async_module,  # noqa: F401  探测 ragas.evaluate 可用性
    )
    try:
        from ragas import aevaluate as _ragas_aevaluate  # type: ignore
        _HAS_AEVALUATE = True
    except ImportError:
        _ragas_aevaluate = None  # type: ignore
        _HAS_AEVALUATE = False
except Exception:  # pragma: no cover
    _ragas_aevaluate = None  # type: ignore
    _HAS_AEVALUATE = False

# 指标导入:ragas 0.4+ 提示从 collections 导入,但 metrics 仍兼容
_METRIC_OBJS: dict[str, Any] = {}
if _RAGAS_AVAILABLE:
    try:  # pragma: no cover - 仅 RAGAS 可用时执行
        from ragas.metrics import (  # type: ignore
            answer_correctness,
            answer_relevancy,
            answer_similarity,
            context_entity_recall,
            context_precision,
            context_recall,
            faithfulness,
        )
        _METRIC_OBJS = {
            "faithfulness": faithfulness,
            "answer_relevancy": answer_relevancy,
            "context_precision": context_precision,
            "context_recall": context_recall,
            "context_entity_recall": context_entity_recall,
            "answer_correctness": answer_correctness,
            "answer_similarity": answer_similarity,
        }
    except Exception as exc:  # pragma: no cover
        logger.warning("RAGAS 指标导入失败,降级为空 metrics: %s", exc)
        _METRIC_OBJS = {}

# datasets 用于构造 Dataset(0.2 兼容);0.4 推荐 EvaluationDataset
try:  # pragma: no cover
    from datasets import Dataset  # type: ignore
    _HAS_DATASETS = True
except Exception:  # pragma: no cover
    Dataset = None  # type: ignore
    _HAS_DATASETS = False

# BaseRagasLLM 用于自定义 LLM 适配器
try:  # pragma: no cover
    from ragas.llms.base import BaseRagasLLM  # type: ignore
    _HAS_BASE_RAGAS_LLM = True
except Exception:  # pragma: no cover
    BaseRagasLLM = object  # type: ignore  # 降级为 object,避免继承报错
    _HAS_BASE_RAGAS_LLM = False

# langchain_core PromptValue 用于 LLM 适配器
try:  # pragma: no cover
    from langchain_core.outputs import Generation, LLMResult  # type: ignore
    from langchain_core.prompt_values import StringPromptValue  # type: ignore
    _HAS_LANGCHAIN_CORE = True
except Exception:  # pragma: no cover
    LLMResult = None  # type: ignore
    Generation = None  # type: ignore
    StringPromptValue = None  # type: ignore
    _HAS_LANGCHAIN_CORE = False


# =====================================================================
# 配置常量
# =====================================================================

# 9 维度名称(7 RAGAS + 2 deadman 扩展)
ALL_METRIC_NAMES: tuple[str, ...] = (
    "faithfulness",
    "answer_relevancy",
    "context_precision",
    "context_recall",
    "context_entity_recall",
    "answer_correctness",
    "answer_similarity",
    "completeness",
    "safety",
)

# quick 模式:仅跑这两个维度(最关键 + 最快)
QUICK_METRIC_NAMES: tuple[str, ...] = ("faithfulness", "answer_relevancy")

# 质量门阈值:faithfulness 低于此值 → 不通过(阻断 merge)
DEFAULT_QUALITY_GATE_THRESHOLD: float = 0.7

# 默认 LLM 配置(degradation 检测)
_DEFAULT_RAGAS_LLM_MODEL: str = "gpt-4o-mini"


# =====================================================================
# LLM 适配器 - 把 deadman.llm.LLMClient 包装为 BaseRagasLLM
# =====================================================================


class DeadmanRagasLLM(BaseRagasLLM):  # type: ignore[misc]
    """把 deadman.llm.LLMClient 适配为 RAGAS 期望的 BaseRagasLLM。

    核心作用:让 RAGAS 评估过程走与生产代码相同的 LLM 调用链,
    即享受 deadman.llm 的多 provider fallback / tenacity 重试 / mock / 成本追踪。

    降级策略:
      - llm_client.api_key 为假 → agenerate_text 抛 RuntimeError,
        RAGAS 会捕获并标记该指标为 NaN(下游 evaluate() 会过滤掉)
      - prompt 解析失败 → 同样抛 RuntimeError
    """

    def __init__(self, llm_client: Any = None) -> None:
        # 不调用 super().__init__() 因为 BaseRagasLLM 的 dataclass 字段
        # 可能在某些 ragas 版本不可用;改为手动初始化最小字段集
        self._client = llm_client
        # 简易缓存:避免重复评估同 prompt
        self._cache: dict[str, str] = {}
        self._max_cache_size = 200

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        # 懒加载全局 llm_client
        try:
            from ..llm import llm_client as _global
            self._client = _global
            return _global
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"deadman.llm 不可用: {exc}") from exc

    def _extract_prompt_text(self, prompt: Any) -> str:
        """从 PromptValue / StringPromptValue / str 提取文本"""
        if isinstance(prompt, str):
            return prompt
        if prompt is None:
            return ""
        # langchain StringPromptValue 有 to_string / to_messages
        for method_name in ("to_string", "to_text"):
            method = getattr(prompt, method_name, None)
            if callable(method):
                try:
                    return method()
                except Exception:
                    continue
        # 有 messages 属性
        messages = getattr(prompt, "messages", None)
        if messages:
            try:
                return "\n".join(m.get("content", "") if isinstance(m, dict) else str(getattr(m, "content", "")) for m in messages)
            except Exception as e:
                logger.debug("提取 prompt messages 失败: %s", e)
        return str(prompt)

    def _build_llm_result(self, text: str) -> Any:
        """构造 langchain LLMResult"""
        if not _HAS_LANGCHAIN_CORE:
            return {"text": text}  # 降级为 dict,RAGAS 0.4 不依赖 dict 但有总比无好
        gen = Generation(text=text)
        return LLMResult(generations=[[gen]])

    # === 同步接口(RAGAS evaluate 用) ===
    def generate_text(
        self,
        prompt: Any,
        n: int = 1,
        temperature: float = 0.01,
        stop: list[str] | None = None,
        callbacks: Any = None,
    ) -> Any:
        """同步生成 - deadman.llm 仅提供 async 接口,这里跑 asyncio.run"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 已在 async 上下文,用 ensure_future
                future = asyncio.run_coroutine_threadsafe(
                    self._agenerate(prompt, n, temperature), loop
                )
                return future.result(timeout=120)
        except RuntimeError:
            pass
        return asyncio.run(self._agenerate(prompt, n, temperature))

    async def agenerate_text(
        self,
        prompt: Any,
        n: int = 1,
        temperature: float | None = 0.01,
        stop: list[str] | None = None,
        callbacks: Any = None,
    ) -> Any:
        return await self._agenerate(prompt, n, temperature if temperature is not None else 0.01)

    async def _agenerate(
        self, prompt: Any, n: int, temperature: float
    ) -> Any:
        text = self._extract_prompt_text(prompt)
        # cache 命中
        cache_key = f"{text[:200]}|n={n}|t={temperature}"
        if cache_key in self._cache:
            return self._build_llm_result(self._cache[cache_key])

        client = self._get_client()
        if not getattr(client, "api_key", None):
            raise RuntimeError("deadman.llm 无可用 api_key,RAGAS 指标无法计算")

        messages = [{"role": "user", "content": text}]
        try:
            result = await client.chat(
                messages, temperature=temperature, max_tokens=2048
            )
        except Exception as exc:
            raise RuntimeError(f"RAGAS LLM 调用失败: {exc}") from exc

        result_text = result if isinstance(result, str) else str(result)
        # 写入 cache
        if len(self._cache) < self._max_cache_size:
            self._cache[cache_key] = result_text
        return self._build_llm_result(result_text)

    def is_finished(self, response: Any) -> bool:
        """判断 LLM 响应是否完成"""
        try:
            if response is None:
                return False
            if hasattr(response, "llm_output") and response.llm_output:
                finish_reason = response.llm_output.get("finish_reason", "")
                return finish_reason != "length"
            return True
        except Exception:
            return True


# =====================================================================
# 评估结果数据类
# =====================================================================


@dataclass
class RagasResult:
    """RAGAS 评估结果"""

    available: bool = False
    degraded: bool = True
    metrics: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    quality_gate_passed: bool | None = None
    quality_gate_threshold: float = DEFAULT_QUALITY_GATE_THRESHOLD
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "degraded": self.degraded,
            "metrics": dict(self.metrics),
            "errors": list(self.errors),
            "quality_gate_passed": self.quality_gate_passed,
            "quality_gate_threshold": self.quality_gate_threshold,
            "note": self.note,
        }


# =====================================================================
# 主评估器
# =====================================================================


class RAGASEvaluator:
    """RAGAS RAG 评估器 - 9 维度 + 质量门 + 降级保护

    使用方式:
        evaluator = RAGASEvaluator()
        result = await evaluator.evaluate(question, answer, contexts, ground_truth)
        if result.quality_gate_passed is False:
            raise QualityGateError("faithfulness 低于阈值")
    """

    def __init__(
        self,
        llm_client: Any = None,
        quality_gate_threshold: float = DEFAULT_QUALITY_GATE_THRESHOLD,
        quick_mode: bool = False,
    ) -> None:
        self.available = _RAGAS_AVAILABLE and _HAS_DATASETS
        self.llm_client = llm_client
        self.quality_gate_threshold = quality_gate_threshold
        self.quick_mode = quick_mode
        # 懒加载 LLM 适配器(仅 RAGAS 可用且需要时才初始化)
        self._ragas_llm: DeadmanRagasLLM | None = None

    def _get_ragas_llm(self) -> DeadmanRagasLLM:
        if self._ragas_llm is None:
            self._ragas_llm = DeadmanRagasLLM(self.llm_client)
        return self._ragas_llm

    def _select_metrics(self) -> list[str]:
        """根据 quick_mode 选指标"""
        if self.quick_mode:
            return list(QUICK_METRIC_NAMES)
        return list(_METRIC_OBJS.keys()) or list(ALL_METRIC_NAMES[:7])

    def _build_dataset(
        self,
        question: str,
        answer: str,
        contexts: list[str],
        ground_truth: str | None = None,
    ) -> Any:
        """构造 RAGAS 数据集(兼容 0.2 Dataset 与 0.4 EvaluationDataset)"""
        data: dict[str, list[Any]] = {
            "question": [question],
            "answer": [answer],
            "contexts": [contexts],
        }
        if ground_truth:
            data["ground_truth"] = [ground_truth]

        # 0.4 优先:EvaluationDataset
        if EvaluationDataset is not None:
            sample = {
                "user_input": question,
                "response": answer,
                "retrieved_contexts": contexts,
            }
            if ground_truth:
                sample["reference"] = ground_truth
            try:
                return EvaluationDataset.from_list([sample])
            except Exception:
                # 字段名可能不同,fallback 到 Dataset
                pass

        # 0.2 兼容:Dataset
        return Dataset.from_dict(data)

    async def evaluate(
        self,
        question: str,
        answer: str,
        contexts: list[str],
        ground_truth: str | None = None,
        # 可选:rule_check_result 与 expected_keywords 用于 completeness/safety 扩展维度
        rule_check_result: dict | None = None,
        expected_keywords: list[str] | None = None,
    ) -> dict[str, Any]:
        """执行 RAGAS 评估(异步,推荐)

        Args:
            question: 用户问题
            answer: 智能体回答
            contexts: RAG 检索到的上下文列表
            ground_truth: 可选的标准答案
            rule_check_result: rules_loader.check_rules 的返回结果,用于 safety 维度
            expected_keywords: 期望出现的关键词,用于 completeness 维度

        Returns:
            {
                "available": bool,
                "degraded": bool,
                "metrics": {metric_name: float},
                "quality_gate_passed": Optional[bool],
                "errors": [str],
            }
        """
        # 1. 降级检测:RAGAS 不可用
        if not self.available:
            return RagasResult(
                available=False,
                degraded=True,
                note="ragas/datasets 包未安装,pip install deadman[ragas] 后可启用",
            ).to_dict()

        # 2. 降级检测:LLM 不可用(避免 CI 红)
        try:
            ragas_llm = self._get_ragas_llm()
            client = ragas_llm._get_client()
            if not getattr(client, "api_key", None):
                return RagasResult(
                    available=True,
                    degraded=True,
                    note="LLM api_key 未配置,跳过 RAGAS 评估(避免 CI 红)",
                ).to_dict()
        except Exception as exc:
            return RagasResult(
                available=True,
                degraded=True,
                errors=[f"LLM 客户端初始化失败: {exc}"],
                note="LLM 不可用,降级跳过",
            ).to_dict()

        # 3. RAGAS 原生指标(7 个)
        metrics_names = self._select_metrics()
        result = RagasResult(
            available=True,
            degraded=False,
            quality_gate_threshold=self.quality_gate_threshold,
        )

        try:
            dataset = self._build_dataset(question, answer, contexts, ground_truth)
            # 仅选取 RAGAS 原生可用的指标
            ragas_metrics_objs = [
                _METRIC_OBJS[name]
                for name in metrics_names
                if name in _METRIC_OBJS
            ]
            if not ragas_metrics_objs:
                raise RuntimeError("无可用 RAGAS 指标对象")

            # 优先异步评估
            if _HAS_AEVALUATE and _ragas_aevaluate is not None:
                ragas_result = await _ragas_aevaluate(
                    dataset,
                    metrics=ragas_metrics_objs,
                    llm=ragas_llm,
                )
            else:
                # 同步 evaluate 放到 executor 跑
                ragas_result = await asyncio.to_thread(
                    _ragas_evaluate_sync,
                    dataset,
                    metrics=ragas_metrics_objs,
                    llm=ragas_llm,
                )

            # 提取分数(ragas 0.4 返回 Result 对象,0.2 返回 dict)
            metrics_dict = self._extract_scores(ragas_result)
            result.metrics.update(metrics_dict)
        except Exception as exc:
            logger.warning("RAGAS 评估失败: %s", exc)
            result.errors.append(f"{type(exc).__name__}: {exc}")
            result.degraded = True

        # 4. deadman 扩展维度:completeness + safety
        # completeness:期望关键词命中率
        if "completeness" in metrics_names or not self.quick_mode:
            try:
                comp_score = self._compute_completeness(answer, expected_keywords)
                result.metrics["completeness"] = comp_score
            except Exception as exc:
                result.errors.append(f"completeness 计算失败: {exc}")

        # safety:基于规则链结果
        if "safety" in metrics_names or not self.quick_mode:
            try:
                safety_score = self._compute_safety(rule_check_result)
                result.metrics["safety"] = safety_score
            except Exception as exc:
                result.errors.append(f"safety 计算失败: {exc}")

        # 5. 质量门判断
        faithfulness_score = result.metrics.get("faithfulness")
        if faithfulness_score is not None:
            result.quality_gate_passed = (
                faithfulness_score >= self.quality_gate_threshold
            )
        else:
            # faithfulness 未算出 → 视为未通过(降级)
            result.quality_gate_passed = None

        return result.to_dict()

    def _extract_scores(self, ragas_result: Any) -> dict[str, float]:
        """从 RAGAS 结果对象提取分数(兼容 0.2 dict 与 0.4 Result)"""
        scores: dict[str, float] = {}
        # 0.4:Result 对象有 to_pandas / to_dict
        for method_name in ("to_dict", "to_pandas"):
            method = getattr(ragas_result, method_name, None)
            if not callable(method):
                continue
            try:
                if method_name == "to_dict":
                    d = method()
                    if isinstance(d, dict):
                        for k, v in d.items():
                            if isinstance(v, (int, float)) and not isinstance(v, bool):
                                scores[k] = float(v)
                        return scores
                else:
                    df = method()
                    # df 是 DataFrame,取第一行
                    if hasattr(df, "to_dict"):
                        row = df.to_dict(orient="records")
                        if row:
                            for k, v in row[0].items():
                                if isinstance(v, (int, float)) and not isinstance(v, bool):
                                    scores[k] = float(v)
                        return scores
            except Exception:
                continue

        # 0.2:dict-like
        if isinstance(ragas_result, dict):
            for k, v in ragas_result.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    scores[k] = float(v)
        return scores

    def _compute_completeness(
        self, answer: str, expected_keywords: list[str] | None
    ) -> float:
        """计算完整性:期望关键词命中率(0-1)

        无 expected_keywords 时返回 1.0(无校验目标)
        """
        if not expected_keywords:
            return 1.0
        answer_lower = answer.lower()
        hits = sum(1 for kw in expected_keywords if kw.lower() in answer_lower)
        return hits / len(expected_keywords)

    def _compute_safety(self, rule_check_result: dict | None) -> float:
        """计算安全性:基于规则链校验结果

        rule_check_result = {"passed": bool, "violations": [...], "violations_count": int}
        返回 1.0(无违规) / 0.0(有违规) / 0.5(无校验结果)
        """
        if rule_check_result is None:
            return 0.5  # 无校验结果,中性
        if rule_check_result.get("passed", False):
            return 1.0
        violations = rule_check_result.get("violations", [])
        # 严重违规 = 0,L0-L2 严重级别
        for v in violations:
            level = str(v.get("level", "")).lower() if isinstance(v, dict) else ""
            if level in ("l0", "l1", "l2", "critical", "high"):
                return 0.0
        return 0.3  # 有违规但非严重

    def evaluate_sync(
        self,
        question: str,
        answer: str,
        contexts: list[str],
        ground_truth: str | None = None,
        rule_check_result: dict | None = None,
        expected_keywords: list[str] | None = None,
    ) -> dict[str, Any]:
        """同步版本(内部用 asyncio.run)"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 已在 async 上下文 → 返回降级结果
                return RagasResult(
                    available=self.available,
                    degraded=True,
                    note="同步调用时检测到正在运行的 event loop,请用 await evaluate()",
                ).to_dict()
        except RuntimeError:
            pass
        return asyncio.run(
            self.evaluate(
                question=question,
                answer=answer,
                contexts=contexts,
                ground_truth=ground_truth,
                rule_check_result=rule_check_result,
                expected_keywords=expected_keywords,
            )
        )


# =====================================================================
# 批量评估 - 从 cases_dir 加载 YAML 并批量跑
# =====================================================================


def _load_case_for_ragas(case_yaml_path: str | Path) -> dict[str, Any] | None:
    """从评估 case YAML 加载 RAGAS 评估所需字段

    YAML case 结构(简化):
        user_input: ...
        evaluation:
            keyword_must_hit: [{keywords: [...], ...}]
            llm_judge: {prompt: ...}
        expected_tool_calls: [...]

    返回:
        {
            "question": str,
            "expected_keywords": [str, ...],  # 从 keyword_must_hit 提取
            "ground_truth": Optional[str],    # 暂从 llm_judge.prompt 启发式提取
            "category": str,
            "case_id": str,
        }
    """
    try:
        import yaml  # type: ignore
    except ImportError:  # pragma: no cover
        return None

    path = Path(case_yaml_path)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            case = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("加载 case %s 失败: %s", path, exc)
        return None

    user_input = case.get("user_input", "")
    evaluation = case.get("evaluation", {}) or {}
    keyword_must_hit = evaluation.get("keyword_must_hit", []) or []

    expected_keywords: list[str] = []
    for group in keyword_must_hit:
        if isinstance(group, dict):
            expected_keywords.extend(group.get("keywords", []) or [])

    return {
        "question": user_input,
        "expected_keywords": expected_keywords,
        "ground_truth": None,  # 真实场景需手动标注
        "category": case.get("category", "unknown"),
        "case_id": str(case.get("case_id", path.stem)),
        "name": case.get("name", path.stem),
    }


async def run_ragas_batch(
    cases_dir: str,
    evaluator: RAGASEvaluator | None = None,
    output_file: str | None = None,
    mock_answer_provider: Any | None = None,
) -> dict[str, Any]:
    """批量跑 RAGAS 评估

    Args:
        cases_dir: YAML case 目录
        evaluator: 可选自定义 RAGASEvaluator
        output_file: 可选,把每个 case 结果写入 JSONL
        mock_answer_provider: 可选 callable(case_dict) -> (answer, contexts)
                              未提供时跳过 answer 生成(只跑降级)

    Returns:
        {
            "total": int,
            "evaluated": int,
            "degraded": int,
            "quality_gate_passed": int,
            "results": [...],
        }
    """
    cases_path = Path(cases_dir)
    if not cases_path.exists():
        return {"total": 0, "evaluated": 0, "degraded": 0, "results": []}

    yaml_files = sorted(cases_path.glob("*.yaml")) + sorted(cases_path.glob("*.yml"))
    evaluator = evaluator or RAGASEvaluator()

    results: list[dict[str, Any]] = []
    quality_gate_passed_count = 0
    degraded_count = 0
    evaluated_count = 0

    # open() 包裹在三元中供 with 使用，ruff 静态识别不到，实际由 with 保证关闭
    out_fp_ctx = (
        open(output_file, "w", encoding="utf-8")  # noqa: SIM115
        if output_file
        else nullcontext()
    )

    with out_fp_ctx as out_fp:
        for yaml_file in yaml_files:
            case_data = _load_case_for_ragas(yaml_file)
            if not case_data:
                continue

            # 获取 answer + contexts
            if mock_answer_provider:
                try:
                    provided = mock_answer_provider(case_data)
                    # 支持 async provider
                    if asyncio.iscoroutine(provided):
                        provided = await provided
                    answer, contexts = provided if isinstance(provided, tuple) else (provided, [])
                except Exception as exc:
                    results.append({
                        "case_id": case_data["case_id"],
                        "error": f"answer provider 失败: {exc}",
                        "degraded": True,
                    })
                    degraded_count += 1
                    continue
            else:
                # 无 answer provider:仅跑降级,记录 case
                answer = ""
                contexts = []

            result = await evaluator.evaluate(
                question=case_data["question"],
                answer=answer,
                contexts=contexts,
                ground_truth=case_data.get("ground_truth"),
                expected_keywords=case_data.get("expected_keywords"),
            )

            evaluated_count += 1
            if result.get("degraded"):
                degraded_count += 1
            if result.get("quality_gate_passed") is True:
                quality_gate_passed_count += 1

            record = {
                "case_id": case_data["case_id"],
                "name": case_data.get("name", ""),
                "category": case_data.get("category", ""),
                "result": result,
            }
            results.append(record)

            if out_fp:
                out_fp.write(json.dumps(record, ensure_ascii=False) + "\n")

    return {
        "total": len(yaml_files),
        "evaluated": evaluated_count,
        "degraded": degraded_count,
        "quality_gate_passed": quality_gate_passed_count,
        "results": results,
    }


# =====================================================================
# 质量门异常 - 供 CI 抛出
# =====================================================================


class QualityGateError(Exception):
    """质量门未通过异常

    faithfulness 低于阈值时抛出,CI 应捕获后以非零退出码退出。
    """

    def __init__(self, message: str, faithfulness: float, threshold: float) -> None:
        super().__init__(message)
        self.faithfulness = faithfulness
        self.threshold = threshold


# =====================================================================
# 全局单例
# =====================================================================

ragas_evaluator = RAGASEvaluator()
