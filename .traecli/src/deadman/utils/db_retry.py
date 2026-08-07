"""best-effort DB 写的并发重试助手。

## 背景

企业级扩展④f-④j 为 cron / notification / vault / ending_note / deadman_switch
五个模块加了「文件存储为 source of truth + DB 双写」。双写统一走各类的
``_run_async()``：

```python
try:
    asyncio.get_running_loop()
    asyncio.ensure_future(coro)   # 已在事件循环 → fire-and-forget
except RuntimeError:
    asyncio.run(coro)             # 同步上下文 → 阻塞执行
```

fire-and-forget 分支不保留 task 引用、不 await 结果，因此**同一个业务动作派发
的多次 DB 写会真正并发执行**。例如 ``SwitchStore.record_check_in()`` 会先派发
check-in INSERT，紧接着 ``save()`` 又派发 switch upsert，两者落在同一事件循环
的同一批次里。

## 缺陷

各同步方法原本是 ``get-then-add`` 形式的 upsert，外面套一个
``except Exception: logger.warning(...)``。并发下必然出现两类**瞬时**冲突：

- ``IntegrityError``：两个协程同时 ``session.get()`` 到 ``None``，各自 INSERT，
  后提交者撞主键唯一约束。
- ``OperationalError: database table is locked``：SQLite shared-cache 下两个
  连接各持读锁并试图升级为写锁，互相阻塞。

两者都被 ``except Exception`` 静默吞掉 —— 写入丢失且无任何感知。实测
（15 轮并发压测，``sqlite+aiosqlite:///file::memory:?cache=shared``）：

| 模块 | 触发冲突 | 记录丢失 |
| --- | --- | --- |
| deadman_switch | 15/15 | ~1/3 概率整条 switch 未落库 |
| ending_note (note/share) | 15/15 | 后写内容被丢弃（DB 留旧值） |
| vault | 15/15 | 后写内容被丢弃 |
| notification guardrail | 15/15 | 15/15 少写记录 |

## 修复

两类冲突退避后重试即可收敛：upsert 重试时会重新 ``session.get()``，此时竞争方
通常已提交，自动改走 UPDATE 分支；纯 INSERT 重试时锁已释放。

调用方需把**整段 DB 操作（含 ORM 对象构造）**包进 ``op`` 闭包，保证每次重试都
构造全新的 ORM 实例 —— 复用已 flush 失败的实例会带着脏 session 状态。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

__all__ = ["DEFAULT_MAX_ATTEMPTS", "best_effort_db_write"]

DEFAULT_MAX_ATTEMPTS = 5
"""默认重试次数。线性退避 0.02s 起，5 次累计最多等待 0.2s。"""

_BACKOFF_BASE_SECONDS = 0.02

_transient_cache: tuple[type[BaseException], ...] | None = None


def _transient_exc_types() -> tuple[type[BaseException], ...]:
    """并发瞬时冲突异常类型（sqlalchemy 属可选依赖，缺失时返回空元组）。"""
    global _transient_cache
    if _transient_cache is None:
        try:
            from sqlalchemy.exc import IntegrityError, OperationalError

            _transient_cache = (IntegrityError, OperationalError)
        except ImportError:
            _transient_cache = ()
    return _transient_cache


async def best_effort_db_write(
    op: Callable[[], Awaitable[None]],
    desc: str,
    logger: logging.Logger,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> bool:
    """执行一次 best-effort DB 写，对并发瞬时冲突退避重试。

    Args:
        op: 无参协程工厂。**每次调用都必须重新构造 ORM 实例**（见模块文档）。
        desc: 失败日志里的动作描述，例如 "同步 switch 到 DB"。
        logger: 调用方模块的 logger，保证日志归属正确的 logger name。
        max_attempts: 最大尝试次数（含首次）。

    Returns:
        True 表示写入成功；False 表示已放弃（已记 warning，不抛异常）。
    """
    transient = _transient_exc_types()

    for attempt in range(max_attempts):
        try:
            await op()
            return True
        except transient as exc:  # 空元组时该分支永不命中
            if attempt + 1 >= max_attempts:
                logger.warning(
                    "%s失败（best-effort，已重试 %d 次）: %s", desc, max_attempts, exc
                )
                return False
            # 线性退避，把时间片让给竞争方完成提交
            await asyncio.sleep(_BACKOFF_BASE_SECONDS * (attempt + 1))
        except Exception as exc:
            logger.warning("%s失败（best-effort）: %s", desc, exc)
            return False
    return False
