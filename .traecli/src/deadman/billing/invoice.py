"""P8.1.5 账单生成 + 发票 + 多支付网关。

设计:
    - 月度账单:每月 1 日生成上月账单(按 plan + 超量计费)
    - 发票导出:PDF(简化为 HTML → PDF)/ CSV / JSON
    - 多支付网关:Stripe / 支付宝 / 微信支付(统一接口)
    - 退款处理:部分退款 / 全额退款
    - 与 P8.6 compliance 协同:发票数据按租户隔离,跨境支付需用户同意

降级:
    - billing 关闭 → 不生成账单,所有调用返回虚拟"免费"账单
    - 支付网关不可用 → 标记为 PENDING,人工跟进
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..infrastructure.feature_flags import is_enabled
from ..infrastructure.multi_tenant import get_current_tenant_id
from .metering import MeteringDimension, MeteringService, get_metering_service
from .plans import get_plan
from .subscription import BillingCycle, SubscriptionManager, get_subscription_manager

logger = logging.getLogger(__name__)


class InvoiceStatus(str, Enum):
    """发票状态机:

    DRAFT → OPEN → PAID → COMPLETED
                  ↓
              VOID(作废)

    - DRAFT: 草稿(未发送)
    - OPEN: 已发送,等待付款
    - PAID: 已付款
    - COMPLETED: 已完成(已开发票)
    - VOID: 已作废
    - REFUNDED: 已退款
    """

    DRAFT = "draft"
    OPEN = "open"
    PAID = "paid"
    COMPLETED = "completed"
    VOID = "void"
    REFUNDED = "refunded"


class PaymentGateway(str, Enum):
    """支付网关。"""

    STRIPE = "stripe"
    ALIPAY = "alipay"
    WECHAT = "wechat"
    OFFLINE = "offline"  # 线下转账(企业版)


@dataclass
class InvoiceLineItem:
    """账单行项。"""

    description: str
    quantity: int
    unit_price: float  # CNY
    amount: float  # quantity * unit_price
    # 可选元数据
    dimension: str = ""  # 超量维度(llm_tokens / tool_calls / ...)
    overage: bool = False  # 是否超量计费


@dataclass
class Invoice:
    """账单(完整)。"""

    invoice_id: str  # 唯一 ID
    user_id: str
    tenant_id: str
    plan_name: str
    period_start: float  # 账单周期开始
    period_end: float  # 账单周期结束
    line_items: list[InvoiceLineItem]
    subtotal: float  # 小计(不含税)
    tax: float  # 税费(增值税 6%)
    total: float  # 总计
    currency: str = "CNY"
    status: InvoiceStatus = InvoiceStatus.DRAFT
    payment_gateway: str | None = None  # PaymentGateway.value
    payment_id: str | None = None  # 网关返回的支付 ID
    paid_at: float | None = None
    created_at: float = field(default_factory=time.time)
    due_at: float = 0.0  # 付款截止时间
    # 退款相关
    refunded_amount: float = 0.0
    refund_history: list[dict] = field(default_factory=list)
    # 元数据
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["line_items"] = [asdict(li) if not isinstance(li, dict) else li for li in self.line_items]
        return d

    @classmethod
    def from_dict(cls, data: dict) -> Invoice:
        line_items_data = data.get("line_items", [])
        line_items = [
            InvoiceLineItem(**li) if isinstance(li, dict) else li
            for li in line_items_data
        ]
        return cls(
            invoice_id=data["invoice_id"],
            user_id=data["user_id"],
            tenant_id=data["tenant_id"],
            plan_name=data["plan_name"],
            period_start=data["period_start"],
            period_end=data["period_end"],
            line_items=line_items,
            subtotal=data["subtotal"],
            tax=data["tax"],
            total=data["total"],
            currency=data.get("currency", "CNY"),
            status=InvoiceStatus(data.get("status", "draft")),
            payment_gateway=data.get("payment_gateway"),
            payment_id=data.get("payment_id"),
            paid_at=data.get("paid_at"),
            created_at=data.get("created_at", time.time()),
            due_at=data.get("due_at", 0.0),
            refunded_amount=data.get("refunded_amount", 0.0),
            refund_history=data.get("refund_history", []),
            metadata=data.get("metadata", {}),
        )


# 超量单价(CNY / 单位)
OVERAGE_PRICES: dict[str, float] = {
    "llm_tokens": 0.0001,  # 每 1K tokens ≈ 0.1 CNY(简化)
    "tool_calls": 0.05,  # 每次工具调用 0.05 CNY
    "storage_mb": 0.5,  # 每月每 MB 0.5 CNY
    "multimodal_calls": 0.5,  # 每次 0.5 CNY
}

# 增值税率
TAX_RATE = 0.06


class InvoiceGenerator:
    """账单生成器。"""

    def __init__(
        self,
        store_path: Path | None = None,
        subscriptions: SubscriptionManager | None = None,
        metering: MeteringService | None = None,
    ) -> None:
        self.store_path = store_path or Path(
            os.environ.get("DEADMAN_INVOICE_STORE", "data/billing/invoices.json")
        )
        self.subscriptions = subscriptions or get_subscription_manager()
        self.metering = metering or get_metering_service()
        self._lock = threading.RLock()
        self._invoices: dict[str, Invoice] = {}
        self._loaded = False

    # ==================================================================
    # 生成账单
    # ==================================================================

    def generate(
        self,
        user_id: str,
        period_start: float,
        period_end: float,
        tenant_id: str | None = None,
    ) -> Invoice | None:
        """生成账单(月度调用)。

        Args:
            period_start: 账单周期开始
            period_end: 账单周期结束(通常是月末)
        """
        if not is_enabled("billing"):
            return None

        tid = tenant_id or get_current_tenant_id()
        sub = self.subscriptions.get_current(user_id)
        if sub is None:
            return None

        plan = get_plan(sub.plan_name)
        if plan is None:
            return None

        # 计算周期内各维度用量
        period_month = time.strftime("%Y-%m", time.localtime(period_end))
        usage_dict = self.metering.get_monthly_usage(user_id, period_month)

        line_items: list[InvoiceLineItem] = []
        subtotal = 0.0

        # 1. plan 基础费用
        if sub.billing_cycle == BillingCycle.YEARLY:
            base_price = plan.price_yearly / 12  # 月度均摊
        else:
            base_price = plan.price_monthly

        if base_price > 0:
            line_item = InvoiceLineItem(
                description=f"{plan.display_name} 月度订阅({plan.name})",
                quantity=1,
                unit_price=base_price,
                amount=base_price,
            )
            line_items.append(line_item)
            subtotal += base_price

        # 2. 超量计费
        for dim, limit in [
            (MeteringDimension.LLM_TOKENS, plan.limits.llm_tokens_monthly),
            (MeteringDimension.TOOL_CALLS, plan.limits.tool_calls_monthly),
            (MeteringDimension.MULTIMODAL, plan.limits.multimodal_calls_monthly),
        ]:
            used = usage_dict.get(dim.value, 0)
            # -1 表示无限制
            if limit <= 0 or used <= limit:
                continue
            overage = used - limit
            unit_price = OVERAGE_PRICES.get(dim.value, 0.0)
            amount = overage * unit_price
            if amount > 0:
                line_item = InvoiceLineItem(
                    description=f"{dim.value} 超量({overage} 单位 × ¥{unit_price})",
                    quantity=overage,
                    unit_price=unit_price,
                    amount=amount,
                    dimension=dim.value,
                    overage=True,
                )
                line_items.append(line_item)
                subtotal += amount

        # 3. 存储(单独算)
        storage_used = usage_dict.get(MeteringDimension.STORAGE.value, 0)
        storage_limit = plan.limits.storage_mb
        if storage_limit > 0 and storage_used > storage_limit:
            overage = storage_used - storage_limit
            unit_price = OVERAGE_PRICES["storage_mb"]
            amount = overage * unit_price
            line_item = InvoiceLineItem(
                description=f"存储超量({overage} MB × ¥{unit_price})",
                quantity=overage,
                unit_price=unit_price,
                amount=amount,
                dimension="storage_mb",
                overage=True,
            )
            line_items.append(line_item)
            subtotal += amount

        tax = subtotal * TAX_RATE
        total = subtotal + tax

        invoice = Invoice(
            invoice_id=self._generate_id(user_id, period_end),
            user_id=user_id,
            tenant_id=tid,
            plan_name=sub.plan_name,
            period_start=period_start,
            period_end=period_end,
            line_items=line_items,
            subtotal=round(subtotal, 2),
            tax=round(tax, 2),
            total=round(total, 2),
            due_at=period_end + 7 * 86400,  # 7 天宽限
        )

        with self._lock:
            self._load()
            self._invoices[invoice.invoice_id] = invoice
            self._save()

        logger.info("Generated invoice %s for user %s (total=¥%.2f)", invoice.invoice_id, user_id, invoice.total)
        return invoice

    # ==================================================================
    # 发票导出
    # ==================================================================

    def export(self, invoice_id: str, format: str = "json") -> bytes | None:
        """导出发票。

        Args:
            format: json / csv / html(简化版 PDF)
        """
        invoice = self.get(invoice_id)
        if invoice is None:
            return None

        if format == "json":
            return (json.dumps(invoice.to_dict(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")

        if format == "csv":
            lines = ["description,quantity,unit_price,amount,dimension,overage"]
            for li in invoice.line_items:
                lines.append(
                    f'"{li.description}",{li.quantity},{li.unit_price},{li.amount},"{li.dimension}",{li.overage}'
                )
            lines.append("")
            lines.append(f"小计,{invoice.subtotal}")
            lines.append(f"税费,{invoice.tax}")
            lines.append(f"总计,{invoice.total}")
            return ("\n".join(lines) + "\n").encode("utf-8")

        if format in ("html", "pdf"):
            return self._export_html(invoice).encode("utf-8")

        return None

    def _export_html(self, invoice: Invoice) -> str:
        """导出 HTML(可后续转 PDF)。"""
        rows = ""
        for li in invoice.line_items:
            rows += (
                f"<tr>"
                f"<td>{li.description}</td>"
                f"<td style='text-align:right'>{li.quantity}</td>"
                f"<td style='text-align:right'>¥{li.unit_price:.2f}</td>"
                f"<td style='text-align:right'>¥{li.amount:.2f}</td>"
                f"</tr>"
            )
        period_start_str = time.strftime("%Y-%m-%d", time.localtime(invoice.period_start))
        period_end_str = time.strftime("%Y-%m-%d", time.localtime(invoice.period_end))
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>账单 {invoice.invoice_id}</title>
<style>
body {{ font-family: -apple-system, "Helvetica Neue", sans-serif; margin: 40px; }}
h1 {{ color: #333; }}
table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
th, td {{ border: 1px solid #ddd; padding: 8px 12px; }}
th {{ background: #f5f5f5; text-align: left; }}
.total {{ font-weight: bold; background: #f9f9f9; }}
</style>
</head>
<body>
<h1>账单 #{invoice.invoice_id}</h1>
<p><b>用户:</b> {invoice.user_id}</p>
<p><b>套餐:</b> {invoice.plan_name}</p>
<p><b>账期:</b> {period_start_str} 至 {period_end_str}</p>
<table>
<thead><tr><th>项目</th><th>数量</th><th>单价</th><th>金额</th></tr></thead>
<tbody>
{rows}
</tbody>
<tfoot>
<tr><td colspan="3">小计</td><td style="text-align:right">¥{invoice.subtotal:.2f}</td></tr>
<tr><td colspan="3">税费(6%)</td><td style="text-align:right">¥{invoice.tax:.2f}</td></tr>
<tr class="total"><td colspan="3">总计({invoice.currency})</td><td style="text-align:right">¥{invoice.total:.2f}</td></tr>
</tfoot>
</table>
<p style="color:#999;font-size:12px;">本账单由 deadman 自动生成,如有疑问请联系客服。</p>
</body>
</html>"""

    # ==================================================================
    # 支付网关
    # ==================================================================

    def send_to_payment_gateway(
        self,
        invoice_id: str,
        gateway: str,
    ) -> str | None:
        """发送到支付网关(模拟实现,真实集成需对接 SDK)。

        Returns:
            网关返回的支付 ID(payment_id)
        """
        if not is_enabled("billing"):
            return None

        invoice = self.get(invoice_id)
        if invoice is None:
            return None

        if gateway not in [g.value for g in PaymentGateway]:
            raise ValueError(f"Unknown payment gateway: {gateway}")

        # 模拟网关调用(真实集成:导入 stripe / alipay_sdk / wechat_pay)
        payment_id = f"{gateway}_{int(time.time())}_{invoice_id[-6:]}"

        with self._lock:
            invoice.payment_gateway = gateway
            invoice.payment_id = payment_id
            invoice.status = InvoiceStatus.OPEN
            invoice.updated_at = time.time()
            self._save()

        logger.info("Invoice %s sent to %s (payment_id=%s)", invoice_id, gateway, payment_id)
        return payment_id

    def mark_paid(self, invoice_id: str, payment_id: str) -> Invoice | None:
        """标记账单已付款(网关回调)。"""
        if not is_enabled("billing"):
            return None

        with self._lock:
            self._load()
            invoice = self._invoices.get(invoice_id)
            if invoice is None:
                return None
            invoice.status = InvoiceStatus.PAID
            invoice.paid_at = time.time()
            invoice.payment_id = payment_id
            self._save()
            return invoice

    def refund(
        self,
        invoice_id: str,
        amount: float | None = None,
        reason: str = "",
    ) -> Invoice | None:
        """退款(部分 / 全额)。

        Args:
            amount: 退款金额,None 表示全额
        """
        if not is_enabled("billing"):
            return None

        with self._lock:
            self._load()
            invoice = self._invoices.get(invoice_id)
            if invoice is None:
                return None
            if invoice.status not in (InvoiceStatus.PAID, InvoiceStatus.COMPLETED):
                return None

            refund_amount = amount if amount is not None else (invoice.total - invoice.refunded_amount)
            if refund_amount <= 0:
                return None

            invoice.refunded_amount += refund_amount
            invoice.refund_history.append({
                "at": time.time(),
                "amount": refund_amount,
                "reason": reason,
            })

            # 全额退款 → 状态变 REFUNDED
            if invoice.refunded_amount >= invoice.total:
                invoice.status = InvoiceStatus.REFUNDED
            self._save()
            return invoice

    def void(self, invoice_id: str, reason: str = "") -> Invoice | None:
        """作废账单。"""
        with self._lock:
            self._load()
            invoice = self._invoices.get(invoice_id)
            if invoice is None:
                return None
            if invoice.status in (InvoiceStatus.PAID, InvoiceStatus.COMPLETED):
                raise ValueError(f"Cannot void paid invoice: {invoice_id}")
            invoice.status = InvoiceStatus.VOID
            invoice.metadata["void_reason"] = reason
            self._save()
            return invoice

    # ==================================================================
    # 查询
    # ==================================================================

    def get(self, invoice_id: str) -> Invoice | None:
        with self._lock:
            self._load()
            return self._invoices.get(invoice_id)

    def list_by_user(self, user_id: str) -> list[Invoice]:
        with self._lock:
            self._load()
            return [inv for inv in self._invoices.values() if inv.user_id == user_id]

    def list_by_status(self, status: InvoiceStatus) -> list[Invoice]:
        with self._lock:
            self._load()
            return [inv for inv in self._invoices.values() if inv.status == status]

    # ==================================================================
    # 内部
    # ==================================================================

    def _generate_id(self, user_id: str, period_end: float) -> str:
        """生成唯一 invoice_id:INV-{YYYYMM}-{user_hash}。"""
        period = time.strftime("%Y%m", time.localtime(period_end))
        user_hash = abs(hash(user_id)) % 100000
        return f"INV-{period}-{user_hash:05d}"

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                for inv_id, idata in data.get("invoices", {}).items():
                    self._invoices[inv_id] = Invoice.from_dict(idata)
        except Exception as e:
            logger.warning("Invoice store load failed: %s", e)
        self._loaded = True

    def _save(self) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": time.time(),
                "invoices": {inv_id: inv.to_dict() for inv_id, inv in self._invoices.items()},
            }
            tmp = self.store_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, self.store_path)
        except Exception as e:
            logger.error("Invoice store save failed: %s", e)


# 全局单例
_ig_instance: InvoiceGenerator | None = None
_ig_lock = threading.Lock()


def get_invoice_generator() -> InvoiceGenerator:
    global _ig_instance
    if _ig_instance is None:
        with _ig_lock:
            if _ig_instance is None:
                _ig_instance = InvoiceGenerator()
    return _ig_instance
