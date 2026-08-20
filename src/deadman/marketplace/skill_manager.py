"""Skill Manager - SKILL.md 文件管理与调用模块。

管理 ``skills/`` 目录下的技能目录。每个技能是一个包含 ``SKILL.md`` 文件的子目录,
SKILL.md 使用 YAML frontmatter (name / description / version) + Markdown body 格式。

设计:
    - SkillManager: 技能的 CRUD + 校验 + URL 导入 + 调用上下文组装
    - 持久化: 文件系统 (skills/<name>/SKILL.md)
    - 线程安全: 单实例 ``threading.RLock`` 保护文件读写
    - 原子写: ``.tmp + os.replace`` 防止写入中断导致文件损坏
    - frontmatter 解析: 优先使用 ``python-frontmatter``,
      不可用时回退到 ``PyYAML``,再不可用时用简单正则解析

feature flag: 无 (技能管理始终可用,不受 marketplace feature flag 控制)
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import re
import shutil
import socket
import tempfile
import threading
import urllib.parse

import httpx
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# =====================================================================
# 异常
# =====================================================================
class SkillError(Exception):
    """Skill Manager 统一异常。

    覆盖场景:
        - 技能不存在
        - SKILL.md 格式非法
        - frontmatter 缺失必填字段
        - 文件 IO 错误
        - URL 下载失败
        - SSRF 拦截
    """


# =====================================================================
# SSRF 防护
# =====================================================================
def _assert_safe_url(url: str) -> None:
    """校验 URL 安全性，拦截 SSRF 探测内网/云元数据端点。

    拦截规则：
    * 仅允许 ``http`` / ``https`` scheme
    * 解析 hostname，DNS 解析后逐 IP 校验是否落在保留段
    * 拒绝：回环(127/8, ::1) / 链路本地(169.254/16, fe80::/10) /
      私网(10/8, 172.16/12, 192.168/16, fc00::/7) / 广播(0/8) /
      未分配(240/4) / 文档(192.0.2/24)

    Raises:
        SkillError: URL 不安全时
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SkillError(f"不允许的 URL scheme: {parsed.scheme!r}（仅 http/https）")
    hostname = parsed.hostname
    if not hostname:
        raise SkillError(f"URL 缺少 hostname: {url}")

    # 直接写 IP 的情况（如 http://169.254.169.254/）
    try:
        ip = ipaddress.ip_address(hostname)
        if _is_unsafe_ip(ip):
            raise SkillError(f"不允许的目标地址（保留段）: {hostname}")
        return
    except ValueError:
        pass  # 是域名，继续 DNS 解析

    # DNS 解析所有 A/AAAA 记录，任一落在保留段即拒绝（防 DNS rebinding）
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise SkillError(f"域名解析失败: {hostname} - {exc}") from exc
    for _family, _stype, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_unsafe_ip(ip):
            raise SkillError(f"域名 {hostname} 解析到保留地址 {ip_str}，已拦截")


def _is_unsafe_ip(ip: ipaddress._BaseAddress) -> bool:
    """判断 IP 是否落在不应被 SSRF 访问的保留段。"""
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


# =====================================================================
# Frontmatter 解析 (三级回退)
# =====================================================================
_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n?(.*)",
    re.DOTALL,
)


def _parse_frontmatter_raw(text: str) -> tuple[dict[str, Any], str]:
    """从 SKILL.md 原始文本中分离 frontmatter 与 body。

    Returns:
        (metadata_dict, body_str)

    Raises:
        SkillError: 文本不包含合法的 ``---`` 分隔符
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise SkillError("SKILL.md 格式非法: 未找到 YAML frontmatter 分隔符 (---)")
    fm_text = match.group(1)
    body = match.group(2).strip()

    # --- 尝试 python-frontmatter ---
    try:
        import frontmatter as _fm  # type: ignore[import-not-found]

        post = _fm.loads(text)
        return dict(post.metadata), post.content.strip()
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("python-frontmatter 解析失败, 回退: %s", exc)

    # --- 尝试 PyYAML ---
    try:
        import yaml  # type: ignore[import-untyped]

        meta = yaml.safe_load(fm_text)
        if isinstance(meta, dict):
            return meta, body
        raise ValueError("yaml.safe_load 返回非 dict")
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("PyYAML 解析失败, 回退到手动解析: %s", exc)

    # --- 手动 key: value 解析 ---
    meta = {}
    for line in fm_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 支持 "key: value" 和 "key:value"
        sep_idx = line.find(":")
        if sep_idx == -1:
            continue
        key = line[:sep_idx].strip()
        val = line[sep_idx + 1 :].strip()
        # 去除可能的引号
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        # 尝试数值转换
        if key == "version":
            # version 保持字符串
            pass
        meta[key] = val
    return meta, body


def _build_skill_md(name: str, description: str, content: str, version: str = "1.0") -> str:
    """构造 SKILL.md 文件内容。"""
    lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
        f"version: {version}",
        "---",
        "",
        content,
    ]
    return "\n".join(lines)


# =====================================================================
# 原子文件写入
# =====================================================================
def _atomic_write(path: Path, content: str) -> None:
    """原子写入: 先写 .tmp 再 os.replace, 防止写入中断损坏文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            suffix=".tmp",
            prefix=path.stem + "_",
            dir=str(path.parent),
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None  # fdopen 已接管
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        logger.debug("原子写入完成: %s", path)
    except OSError as exc:
        logger.error("原子写入失败 (%s): %s", path, exc)
        # 清理残留 tmp
        if tmp_path and tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise SkillError(f"文件写入失败: {path} - {exc}") from exc
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


# =====================================================================
# SkillManager
# =====================================================================
class SkillManager:
    """SKILL.md 技能管理器。

    线程安全: 单实例 ``threading.RLock`` 保护所有文件操作。
    原子写: 通过 ``_atomic_write`` (.tmp + os.replace) 保证写入完整性。

    Args:
        skills_dir: 技能根目录。为 None 时从 config.settings.skills_dir 读取,
                    再失败则回退到 ``~/.deadman/skills``。
    """

    SKILL_FILENAME = "SKILL.md"

    def __init__(self, skills_dir: Path | None = None) -> None:
        if skills_dir is not None:
            self._skills_dir = Path(skills_dir)
        else:
            self._skills_dir = self._resolve_default_skills_dir()
        self._lock = threading.RLock()
        logger.info("SkillManager 初始化: skills_dir=%s", self._skills_dir)

    # ------------------------------------------------------------------
    # 路径解析
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_default_skills_dir() -> Path:
        """从 config 读取 skills_dir, 失败则回退 ~/.deadman/skills。"""
        try:
            from ..config import settings  # type: ignore[import-not-found]

            return Path(settings.skills_dir)
        except Exception:
            logger.debug("无法从 config.settings 读取 skills_dir, 回退到 ~/.deadman/skills")
            return Path.home() / ".deadman" / "skills"

    @property
    def skills_dir(self) -> Path:
        """技能根目录 (只读属性)。"""
        return self._skills_dir

    def _skill_dir(self, name: str) -> Path:
        return self._skills_dir / name

    def _skill_md_path(self, name: str) -> Path:
        return self._skill_dir(name) / self.SKILL_FILENAME

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------
    def _read_skill_file(self, name: str) -> tuple[dict[str, Any], str, Path]:
        """读取并解析指定技能的 SKILL.md。

        Returns:
            (metadata, body, skill_md_path)

        Raises:
            SkillError: 技能不存在或格式非法
        """
        md_path = self._skill_md_path(name)
        if not md_path.exists():
            raise SkillError(f"技能 '{name}' 不存在: {md_path}")
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillError(f"读取技能 '{name}' 失败: {exc}") from exc
        meta, body = _parse_frontmatter_raw(text)
        return meta, body, md_path

    @staticmethod
    def _validate_name(name: str) -> None:
        """校验技能名: 仅允许小写字母、数字、连字符。"""
        if not name:
            raise SkillError("技能名不能为空")
        if not re.match(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$", name) and not re.match(
            r"^[a-z0-9]$", name
        ):
            raise SkillError(
                f"技能名 '{name}' 格式非法: 仅允许小写字母、数字、连字符, 且首尾必须为字母或数字"
            )

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    def list_skills(self) -> list[dict]:
        """扫描 skills/*/SKILL.md, 返回所有技能的摘要列表。

        Returns:
            列表, 每项包含:
                - name: 技能名
                - description: 描述
                - version: 版本号
                - path: SKILL.md 绝对路径
                - created_at: 目录创建时间 (epoch)
                - size_bytes: SKILL.md 文件大小
        """
        with self._lock:
            if not self._skills_dir.exists():
                logger.info("技能目录不存在: %s", self._skills_dir)
                return []

            results: list[dict] = []
            for child in sorted(self._skills_dir.iterdir()):
                if not child.is_dir():
                    continue
                md_path = child / self.SKILL_FILENAME
                if not md_path.exists():
                    logger.debug("跳过无 SKILL.md 的目录: %s", child.name)
                    continue
                try:
                    meta, _, _ = self._read_skill_file(child.name)
                    stat = md_path.stat()
                    results.append(
                        {
                            "name": meta.get("name", child.name),
                            "description": meta.get("description", ""),
                            "version": str(meta.get("version", "1.0")),
                            "path": str(md_path),
                            "created_at": stat.st_ctime,
                            "size_bytes": stat.st_size,
                        }
                    )
                except SkillError as exc:
                    logger.warning("跳过无效技能 '%s': %s", child.name, exc)
                    continue

            logger.info("扫描到 %d 个技能", len(results))
            return results

    def get_skill(self, name: str) -> dict | None:
        """读取指定技能的完整内容 (frontmatter + body)。

        Args:
            name: 技能名 (目录名)

        Returns:
            包含以下字段的 dict, 技能不存在时返回 None:
                - name: 技能名
                - description: 描述
                - version: 版本号
                - body: Markdown body 内容
                - metadata: 完整 frontmatter dict
                - path: SKILL.md 绝对路径
                - raw: SKILL.md 原始文本

        Raises:
            SkillError: SKILL.md 存在但格式非法
        """
        with self._lock:
            md_path = self._skill_md_path(name)
            if not md_path.exists():
                logger.info("技能 '%s' 不存在", name)
                return None
            meta, body, _ = self._read_skill_file(name)
            try:
                raw = md_path.read_text(encoding="utf-8")
            except OSError:
                raw = ""
            return {
                "name": meta.get("name", name),
                "description": meta.get("description", ""),
                "version": str(meta.get("version", "1.0")),
                "body": body,
                "metadata": meta,
                "path": str(md_path),
                "raw": raw,
            }

    def create_skill(
        self,
        name: str,
        description: str,
        content: str,
        version: str = "1.0",
    ) -> dict:
        """创建新技能: 建立目录并写入 SKILL.md。

        Args:
            name: 技能名 (将作为目录名, 仅允许 ``[a-z0-9-]``)
            description: 技能描述
            content: Markdown body 内容
            version: 版本号, 默认 "1.0"

        Returns:
            创建成功的技能摘要 dict

        Raises:
            SkillError: 名称非法 / 已存在 / 写入失败
        """
        self._validate_name(name)
        with self._lock:
            skill_dir = self._skill_dir(name)
            md_path = skill_dir / self.SKILL_FILENAME
            if md_path.exists():
                raise SkillError(f"技能 '{name}' 已存在: {md_path}")

            file_content = _build_skill_md(name, description, content, version)
            _atomic_write(md_path, file_content)
            logger.info("技能 '%s' 创建成功: %s", name, md_path)

            stat = md_path.stat()
            return {
                "name": name,
                "description": description,
                "version": str(version),
                "path": str(md_path),
                "created_at": stat.st_ctime,
                "size_bytes": stat.st_size,
            }

    def delete_skill(self, name: str) -> bool:
        """删除技能: 移除整个技能目录。

        Args:
            name: 技能名

        Returns:
            True 表示删除成功

        Raises:
            SkillError: 技能不存在 / 删除失败
        """
        with self._lock:
            skill_dir = self._skill_dir(name)
            if not skill_dir.exists():
                raise SkillError(f"技能 '{name}' 不存在: {skill_dir}")
            if not skill_dir.is_dir():
                raise SkillError(f"路径不是目录: {skill_dir}")

            try:
                shutil.rmtree(skill_dir)
            except OSError as exc:
                raise SkillError(f"删除技能 '{name}' 失败: {exc}") from exc

            logger.info("技能 '%s' 已删除: %s", name, skill_dir)
            return True

    def update_skill(
        self,
        name: str,
        description: str | None = None,
        content: str | None = None,
    ) -> dict:
        """更新技能内容 (description 和/或 body)。

        仅修改传入的字段, 未传入的字段保持不变。

        Args:
            name: 技能名
            description: 新描述, None 表示不修改
            content: 新 Markdown body, None 表示不修改

        Returns:
            更新后的技能摘要 dict

        Raises:
            SkillError: 技能不存在 / 写入失败
        """
        with self._lock:
            meta, body, md_path = self._read_skill_file(name)

            # 合并更新
            if description is not None:
                meta["description"] = description
            if content is not None:
                body = content

            version = str(meta.get("version", "1.0"))
            skill_name = meta.get("name", name)
            desc = meta.get("description", "")

            file_content = _build_skill_md(skill_name, desc, body, version)
            _atomic_write(md_path, file_content)
            logger.info("技能 '%s' 更新成功", name)

            stat = md_path.stat()
            return {
                "name": skill_name,
                "description": desc,
                "version": version,
                "path": str(md_path),
                "created_at": stat.st_ctime,
                "size_bytes": stat.st_size,
            }

    def import_skill_from_url(self, url: str) -> dict:
        """从 URL 下载 SKILL.md 并安装到技能目录。

        下载的文件必须符合 SKILL.md frontmatter 格式, 且 frontmatter 中
        必须包含 ``name`` 字段 (用于确定安装目录名)。

        SSRF 防护：拒绝回环 / 链路本地 / 私网 / 广播地址，仅允许 http/https。

        Args:
            url: SKILL.md 的 HTTP/HTTPS URL

        Returns:
            安装成功的技能摘要 dict

        Raises:
            SkillError: 下载失败 / 格式非法 / name 缺失 / SSRF 拦截
        """
        logger.info("从 URL 导入技能: %s", url)

        # --- SSRF 防护：校验 scheme + 解析后的 IP 不在内网/保留段 ---
        _assert_safe_url(url)

        # --- 下载 ---
        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": "deadman-skill-manager/1.0"},
                timeout=30,
            )
            resp.raise_for_status()
            raw_text = resp.text
        except httpx.HTTPStatusError as exc:
            raise SkillError(f"下载失败 (HTTP {exc.response.status_code}): {url}") from exc
        except httpx.HTTPError as exc:
            raise SkillError(f"下载失败 (网络错误): {url} - {exc}") from exc
        except Exception as exc:
            raise SkillError(f"下载失败: {url} - {exc}") from exc

        # --- 解析 ---
        meta, body = _parse_frontmatter_raw(raw_text)
        skill_name = meta.get("name")
        if not skill_name:
            raise SkillError("导入失败: SKILL.md frontmatter 中缺少 'name' 字段")
        # 规范化名称
        skill_name = re.sub(r"[^a-z0-9\-]", "-", skill_name.lower()).strip("-")
        if not skill_name:
            raise SkillError("导入失败: frontmatter 中的 name 字段无效")

        description = meta.get("description", "")
        version = str(meta.get("version", "1.0"))

        with self._lock:
            md_path = self._skill_md_path(skill_name)
            file_content = _build_skill_md(skill_name, description, body, version)
            _atomic_write(md_path, file_content)
            logger.info("技能 '%s' 从 URL 导入成功: %s", skill_name, url)

            stat = md_path.stat()
            return {
                "name": skill_name,
                "description": description,
                "version": version,
                "path": str(md_path),
                "created_at": stat.st_ctime,
                "size_bytes": stat.st_size,
                "source_url": url,
            }

    def validate_skill(self, name: str) -> dict:
        """校验技能的 SKILL.md 是否合法。

        检查项:
            - 技能目录和 SKILL.md 是否存在
            - frontmatter 是否包含 name / description / version
            - body 是否非空
            - frontmatter 中的 name 是否与目录名一致

        Args:
            name: 技能名

        Returns:
            校验结果 dict:
                - valid: bool - 是否通过全部校验
                - name: str - 技能名
                - errors: list[str] - 错误列表
                - warnings: list[str] - 警告列表
        """
        errors: list[str] = []
        warnings: list[str] = []

        with self._lock:
            skill_dir = self._skill_dir(name)
            md_path = skill_dir / self.SKILL_FILENAME

            # --- 存在性 ---
            if not skill_dir.exists():
                return {
                    "valid": False,
                    "name": name,
                    "errors": [f"技能目录不存在: {skill_dir}"],
                    "warnings": [],
                }
            if not md_path.exists():
                return {
                    "valid": False,
                    "name": name,
                    "errors": [f"SKILL.md 不存在: {md_path}"],
                    "warnings": [],
                }

            # --- 解析 ---
            try:
                meta, body, _ = self._read_skill_file(name)
            except SkillError as exc:
                return {
                    "valid": False,
                    "name": name,
                    "errors": [str(exc)],
                    "warnings": [],
                }

            # --- frontmatter 完整性 ---
            if not meta.get("name"):
                errors.append("frontmatter 缺少 'name' 字段")
            elif meta["name"] != name:
                warnings.append(
                    f"frontmatter 中的 name ('{meta['name']}') 与目录名 ('{name}') 不一致"
                )

            if not meta.get("description"):
                errors.append("frontmatter 缺少 'description' 字段")

            if "version" not in meta:
                warnings.append("frontmatter 缺少 'version' 字段 (将默认为 1.0)")

            # --- body 非空 ---
            if not body or not body.strip():
                errors.append("SKILL.md body 为空")

            valid = len(errors) == 0
            logger.info(
                "技能 '%s' 校验完成: valid=%s errors=%d warnings=%d",
                name,
                valid,
                len(errors),
                len(warnings),
            )
            return {
                "valid": valid,
                "name": name,
                "errors": errors,
                "warnings": warnings,
            }

    def invoke_skill(self, name: str, user_query: str) -> dict:
        """加载技能内容并与用户查询组装为 prompt 上下文。

        不执行技能, 仅返回组装好的 prompt 供调用方使用。

        Args:
            name: 技能名
            user_query: 用户的原始查询文本

        Returns:
            组装结果 dict:
                - skill_name: str - 技能名
                - prompt: str - 组装后的完整 prompt
                - skill_description: str - 技能描述
                - skill_version: str - 技能版本
                - user_query: str - 原始用户查询

        Raises:
            SkillError: 技能不存在 / 格式非法
        """
        with self._lock:
            meta, body, _ = self._read_skill_file(name)

        skill_name = meta.get("name", name)
        description = meta.get("description", "")
        version = str(meta.get("version", "1.0"))

        # --- 组装 prompt ---
        sections = [
            f"# 技能: {skill_name}",
            f"版本: {version}",
            f"描述: {description}",
            "",
            "## 技能指令",
            "",
            body,
            "",
            "## 用户请求",
            "",
            user_query,
        ]
        prompt = "\n".join(sections)

        logger.info(
            "技能 '%s' prompt 组装完成 (prompt_length=%d, query_length=%d)",
            name,
            len(prompt),
            len(user_query),
        )
        return {
            "skill_name": skill_name,
            "prompt": prompt,
            "skill_description": description,
            "skill_version": version,
            "user_query": user_query,
        }


# =====================================================================
# 全局单例
# =====================================================================
_skill_manager_instance: SkillManager | None = None
_skill_manager_lock = threading.Lock()


def get_skill_manager(skills_dir: Path | None = None) -> SkillManager:
    """获取全局 SkillManager 单例。

    首次调用时创建实例, 后续调用返回同一实例。
    可通过 ``skills_dir`` 参数显式指定技能目录 (仅首次调用生效)。

    Args:
        skills_dir: 技能根目录, None 时从 config 读取或回退默认路径

    Returns:
        SkillManager 单例
    """
    global _skill_manager_instance
    if _skill_manager_instance is None:
        with _skill_manager_lock:
            if _skill_manager_instance is None:
                _skill_manager_instance = SkillManager(skills_dir=skills_dir)
    return _skill_manager_instance
