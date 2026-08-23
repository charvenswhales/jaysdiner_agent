import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from db.base import get_db
from db.models import User, Order
from auth.dependencies import get_current_user
from orders.schemas import OrderSummary, OrderDetail

router = APIRouter(prefix="/orders", tags=["orders"])


def _safe_json_list(raw: str) -> list:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def _safe_json_dict(raw: str) -> dict:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


@router.get("", response_model=list[OrderSummary])
def list_orders(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    orders = (
        db.query(Order)
        .filter(Order.user_id == current_user.id)
        .order_by(Order.created_at.desc())
        .all()
    )
    return [
        OrderSummary(
            id=o.id,
            order_identifier=o.order_identifier,
            items=_safe_json_list(o.items_json),
            total_ngn=float(o.total_ngn),
            status=o.status,
            monei_transaction_ref=o.monei_transaction_ref,
            created_at=o.created_at,
        )
        for o in orders
    ]


@router.get("/{order_id}", response_model=OrderDetail)
def get_order(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = (
        db.query(Order)
        .filter(Order.id == order_id, Order.user_id == current_user.id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    return OrderDetail(
        id=order.id,
        order_identifier=order.order_identifier,
        items=_safe_json_list(order.items_json),
        total_ngn=float(order.total_ngn),
        status=order.status,
        monei_transaction_ref=order.monei_transaction_ref,
        created_at=order.created_at,
        checkout_info=_safe_json_dict(order.checkout_info_json) if order.checkout_info_json else None,
        payment_details=_safe_json_dict(order.payment_details_json) if order.payment_details_json else None,
    )