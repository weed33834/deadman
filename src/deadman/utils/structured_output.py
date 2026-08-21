"""结构化输出工具（通用智能体能力，原缺失）。

让智能体产出**可校验的结构化结果**（Pydantic 模型 / JSON Schema），
避免"LLM 返回自由文本、调用方手工解析且易碎"。

- ``parse_json``：容错解析 LLM 返回的 JSON（含 ```json``` 代码块包裹、前后缀噪音）。
- ``validate``：用 Pydantic 模型校验 / 纠偏；失败返回错误，不抛异常。

基于成熟库 pydantic（已是核心依赖），不重复造轮子。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", re.S)


def parse_json(text: str) -> Any | None:
    """容错解析 LLM 返回的 JSON。

    支持：
    - 纯 JSON 字符串
    - ```json ... ``` 代码块包裹
    - 前后有说明文字（尝试提取第一个 { 或 [ 到末尾的平衡 JSON）
    """
    if not text:
        return None
    text = text.strip()
    # 1) 直接解析
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # 2) 代码块
    m = _CODE_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            pass
    # 3) 从第一个 { / [ 截取到末尾
    for start_ch, end_ch in (("{", "}"), ("[", "]")):
        s = text.find(start_ch)
        if s < 0:
            continue
        e = text.rfind(end_ch)
        if e > s:
            try:
                return json.loads(text[s : e + 1])
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def validate(model: type[M], data: Any) -> tuple[M | None, list[str]]:
    """用 Pydantic 模型校验 / 构造数据。

    Returns:
        (模型实例 or None, 错误信息列表)
    """
    if isinstance(data, str):
        data = parse_json(data)
    try:
        return model.model_validate(data), []
    except ValidationError as exc:
        errs = [f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in exc.errors()]
        logger.debug("structured_output 校验失败: %s", errs)
        return None, errs
    except Exception as exc:  # pragma: no cover - 防御
        return None, [str(exc)]
