from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class OrderSummary(BaseModel):
    """List view, no checkout/payment detail, just enough to render a card."""
    id: str
    order_identifier: Optional[str] = None
    items: list[Any] = []
    total_ngn: float
    status: str
    monei_transaction_ref: Optional[str] = None
    created_at: datetime


class OrderDetail(OrderSummary):
    """Single-order view, everything, including what checkout info and
    payment/transfer details were actually used for this specific order."""
    checkout_info: Optional[dict] = None
    payment_details: Optional[dict] = None