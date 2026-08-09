#!/usr/bin/env python3
"""从省级数据表渲染 `src/knowledge/regions/CN/*.md` 知识库。

用法::

    python3 scripts/render_kb_cn.py            # 渲染全部
    python3 scripts/render_kb_cn.py --check    # 只校验，不写盘（CI 用）
    python3 scripts/render_kb_cn.py --only hebei shanxi

设计说明见 `scripts/kb_cn_data.py` 头部：全国统一法条口径写死在渲染模板，
省级差异化事实来自 `Province` 数据表，从根本上避免手写 26 份产生口径漂移。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import kb_cn_data  # noqa: E402
import kb_cn_data_west  # noqa: E402
from kb_cn_data import Province  # noqa: E402
from kb_cn_render_lib import (  # noqa: E402
    emergency,
    header,
    one_thing,
    stage1,
    stage2,
    stage3,
    stage4,
    stage5,
)
from kb_cn_render_lib2 import (  # noqa: E402
    footer,
    medical,
    special,
    stage6,
    stage7,
    stage8,
    stage9,
)

OUT_DIR = SCRIPT_DIR.parent / ".traecli" / "knowledge" / "regions" / "CN"

# beijing.md 骨架要求的一级 / 二级标题（结构校验用）。
REQUIRED_SECTIONS = [
    "## 元信息",
    "## 紧急联系方式",
    "## 阶段1：死亡证明",
    "## 阶段2：遗体处理",
    "## 阶段3：身份注销",
    "## 阶段4：数字账号处理",
    "## 阶段5：金融资产继承",
    "## 阶段6：不动产与车辆",
    "## 阶段7：遗产继承整体",
    "## 阶段8：社保与福利结算",
    "## 阶段9：债权债务",
    "## 特殊情形",
    "## 医疗政策补充（medical-guide 团队使用）",
    "## 数据来源与免责",
]

REQUIRED_SUBSECTIONS = [
    "### 签发机构",
    "### 所需材料",
    "### 流程",
    "### 时限",
    "### 费用",
    "### 遗体安置选项",
    "### 殡葬方式",
    "### 时限要求",
    "### 费用参考",
    "### 殡葬补贴/丧葬费",
    "### 注销机构",
    "### 异地办理",
    "### 注销前必须完成的事项",
    "### 主要平台的逝者通道",
    "### 银行存款",
    "### 证券/股票",
    "### 保险",
    "### 房产过户",
    "### 车辆过户",
    "### 法定继承顺序",
    "### 遗嘱形式",
    "### 继承公证/法院程序",
    "### 遗产税",
    "### 无人继承遗产处理",
    "### 社会保险结算",
    "### 丧葬补助与抚恤金",
    "### 公积金/养老金账户",
    "### 法律原则",
    "### 常见债务处理",
    "### 异地/跨国死亡",
    "### 非正常死亡",
    "### 器官/遗体捐献",
    "### 其他当地特殊情形",
    "### 医保体系概览",
    "### 门诊特殊病种/慢性病备案",
    "### 异地就医",
    "### 大病保险/补充保险",
    "### 商业保险理赔通用流程",
    "### 医疗纠纷处理",
    "### 临终关怀/安宁疗护",
    "### 当地特殊医疗规定",
]


def collect_provinces() -> list[Province]:
    """从两个数据模块收集全部 Province 实例，按定义顺序返回。"""
    found: list[Province] = []
    seen: set[str] = set()
    for module in (kb_cn_data, kb_cn_data_west):
        for name in vars(module):
            if name.startswith("_"):
                continue
            value = getattr(module, name)
            if isinstance(value, Province):
                if value.key in seen:
                    raise ValueError(f"重复的省份 key: {value.key}")
                seen.add(value.key)
                found.append(value)
    return found


def render(p: Province, today: str) -> str:
    lines: list[str] = []
    lines += header(p, today)
    lines += emergency(p)
    lines += one_thing(p)
    lines += stage1(p)
    lines += stage2(p)
    lines += stage3(p)
    lines += stage4(p)
    lines += stage5(p)
    lines += stage6(p)
    lines += stage7(p)
    lines += stage8(p)
    lines += stage9(p)
    lines += special(p)
    lines += medical(p)
    lines += footer(p, today)
    return "\n".join(lines) + "\n"


# 数据纪律：正文中不得出现「疑似编造的本地座机 / 400 号码」。
# 允许清单为全国统一号码与已在 beijing.md 中出现的公开热线。
ALLOWED_NUMBERS = {
    "110",
    "119",
    "120",
    "114",
    "12345",
    "12333",
    "12329",
    "12348",
    "12378",
    "12320",
    "12328",
    "12315",
    "12123",
    "95017",
    "95188",
    "400-161-9995",
    "+86-10-12308",
}


def audit(text: str, p: Province) -> list[str]:
    """结构 + 数据纪律校验，返回问题列表（空表示通过）。"""
    problems: list[str] = []

    for section in REQUIRED_SECTIONS:
        if section not in text:
            problems.append(f"缺少区块: {section}")
    for sub in REQUIRED_SUBSECTIONS:
        if sub not in text:
            problems.append(f"缺少子区块: {sub}")

    if not text.startswith(f"# 中国 - {p.name} 身后事政策"):
        problems.append("标题不符合 SCHEMA")
    if "数据可信度: 中" not in text:
        problems.append("缺少数据可信度标注")
    if p.portal not in text:
        problems.append("正文未引用已核验的省级门户")

    import re

    # 电话号码形态：区号-号码 / 连续 7 位以上数字 / 400 开头
    for match in re.findall(r"\b(?:\d{3,4}-\d{7,8}|\d{7,12}|400-\d{3}-\d{4})\b", text):
        if match in ALLOWED_NUMBERS:
            continue
        # 法条编号、年份、金额等由上下文排除
        problems.append(f"疑似未核验的电话号码: {match}")

    # 只允许出现已核验的 URL
    for url in re.findall(r"https?://[^\s（）()，,、]+", text):
        allowed = url == p.portal or url in {
            "https://www.gov.cn",
            "https://gjzwfw.www.gov.cn",
            "https://www.mca.gov.cn",
            "https://www.mohrss.gov.cn",
            "https://www.nhsa.gov.cn",
            "https://www.mnr.gov.cn",
            "https://www.court.gov.cn",
        }
        if not allowed:
            problems.append(f"未核验的 URL: {url}")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染中国大陆省级身后事知识库")
    parser.add_argument("--check", action="store_true", help="只校验不写盘")
    parser.add_argument("--only", nargs="*", default=None, help="仅处理指定 key")
    parser.add_argument(
        "--date",
        default=None,
        help="覆盖「最后更新」日期（YYYY-MM-DD），缺省用 Asia/Shanghai 当天",
    )
    args = parser.parse_args()

    if args.date:
        today = date.fromisoformat(args.date).isoformat()
    else:
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    provinces = collect_provinces()
    if args.only:
        wanted = set(args.only)
        provinces = [p for p in provinces if p.key in wanted]
        missing = wanted - {p.key for p in provinces}
        if missing:
            print(f"未知的 key: {sorted(missing)}", file=sys.stderr)
            return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    failed = 0
    for p in provinces:
        text = render(p, today)
        problems = audit(text, p)
        if problems:
            failed += 1
            print(f"[FAIL] {p.key}", file=sys.stderr)
            for problem in problems:
                print(f"       - {problem}", file=sys.stderr)
            continue
        target = OUT_DIR / f"{p.key}.md"
        if args.check:
            status = "OK(check)"
        else:
            target.write_text(text, encoding="utf-8")
            status = "written"
        print(f"[{status}] {p.key:<13} {p.name:<12} {len(text.splitlines()):>4} 行")

    print(f"\n合计 {len(provinces)} 个地区，失败 {failed} 个")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
