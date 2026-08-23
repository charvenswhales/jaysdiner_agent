import urllib.parse

import json

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import (
    MONEI_CLIENT_ID,
    MONEI_REDIRECT_URI,
    MONEI_AUTHORIZE_BASE,
    MONEI_SCOPES,
    MONEI_FRONTEND_RETURN_URL,
)
from db.base import get_db
from db.models import User, MoneiConnection, Order
from db.crypto import encrypt_token
from auth.dependencies import get_current_user
from connect.security import create_oauth_state, verify_oauth_state
from connect.monei_client import (
    exchange_code_for_tokens,
    expires_at_from_now,
    get_wallet,
    get_supported_banks,
    send_bank_transfer_payout,
    resolve_bank_code,
    classify_payout_error,
    MoneiTokenError,
    MoneiAPIError,
)
from connect.tokens import get_valid_access_token, NoMoneiConnection

router = APIRouter(prefix="/connect/monei", tags=["monei-connect"])


@router.get("/authorize")
def get_authorize_url(current_user: User = Depends(get_current_user)):
    """Returns the URL for the frontend to navigate the browser to
    (window.location.href = authorize_url), this endpoint itself doesn't
    redirect, since it's called via fetch (needs the JWT header), not a
    plain browser navigation."""
    state = create_oauth_state(current_user.id)
    params = {
        "client_id": MONEI_CLIENT_ID,
        "redirect_uri": MONEI_REDIRECT_URI,
        "scope": MONEI_SCOPES,
        "state": state,
    }
    url = f"{MONEI_AUTHORIZE_BASE}?{urllib.parse.urlencode(params)}"
    return {"authorize_url": url}


@router.get("/callback")
def callback(
    code: str = Query(default=None),
    state: str = Query(default=None),
    error: str = Query(default=None),
    db: Session = Depends(get_db),
):
    """Monei redirects the browser here directly after the user approves
    or denies, there's no Authorization header on this request (it's a
    plain navigation), which is why the user is identified via the signed
    `state` param instead of the usual JWT dependency. See
    connect/security.py."""

    def _return(result: str, detail: str = "") -> RedirectResponse:
        params = {"monei": result}
        if detail:
            params["detail"] = detail
        return RedirectResponse(f"{MONEI_FRONTEND_RETURN_URL}?{urllib.parse.urlencode(params)}")

    if error:
        return _return("denied")

    if not code or not state:
        return _return("error", "missing_code_or_state")

    user_id = verify_oauth_state(state)
    if not user_id:
        return _return("error", "invalid_or_expired_state")

    try:
        tokens = exchange_code_for_tokens(code)
    except MoneiTokenError:
        return _return("error", "token_exchange_failed")

    scopes_granted = tokens.get("scopes") or []
    expires_at = expires_at_from_now(tokens["expires_in"])

    existing = db.query(MoneiConnection).filter(MoneiConnection.user_id == user_id).first()
    if existing:
        existing.access_token_encrypted = encrypt_token(tokens["access_token"])
        existing.refresh_token_encrypted = encrypt_token(tokens["refresh_token"])
        existing.scopes = " ".join(scopes_granted)
        existing.expires_at = expires_at
    else:
        db.add(MoneiConnection(
            user_id=user_id,
            access_token_encrypted=encrypt_token(tokens["access_token"]),
            refresh_token_encrypted=encrypt_token(tokens["refresh_token"]),
            scopes=" ".join(scopes_granted),
            expires_at=expires_at,
        ))
    db.commit()

    # Partial grants, user can approve fewer scopes than requested. Without
    # wallet:withdraw, we can't actually pay on their behalf later.
    if "wallet:withdraw" not in scopes_granted:
        return _return("partial", "wallet_withdraw_not_granted")

    return _return("connected")


@router.get("/status")
def connection_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conn = db.query(MoneiConnection).filter(MoneiConnection.user_id == current_user.id).first()
    if not conn:
        return {"connected": False}
    return {
        "connected": True,
        "scopes": conn.scopes.split(" ") if conn.scopes else [],
        "expires_at": conn.expires_at,
    }


@router.delete("/disconnect")
def disconnect(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conn = db.query(MoneiConnection).filter(MoneiConnection.user_id == current_user.id).first()
    if conn:
        db.delete(conn)
        db.commit()
    return {"connected": False}


@router.get("/balance")
def get_balance(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        access_token = get_valid_access_token(current_user.id, db)
    except NoMoneiConnection:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "not_connected", "message": "Monei wallet not connected"},
        )
    except MoneiTokenError:
        raise HTTPException(
            status_code=502,
            detail={"error_code": "session_expired", "message": "Could not refresh Monei session, try reconnecting"},
        )

    try:
        wallet = get_wallet(access_token)
    except MoneiAPIError:
        raise HTTPException(
            status_code=502,
            detail={"error_code": "wallet_unavailable", "message": "Could not reach Monei to fetch wallet balance"},
        )

    naira_balance = (wallet.get("data") or {}).get("nairaBalance", 0)
    return {"naira_balance": naira_balance}


class PayOrderRequest(BaseModel):
    transaction_pin: str


@router.post("/pay-order/{order_id}")
def pay_order(
    order_id: str,
    payload: PayOrderRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Pays a previously placed order directly from the user's Monei
    wallet. transaction_pin comes straight from a dedicated frontend
    input, it must NEVER be routed through the chat/agent, since that
    would put a secret credential into LLM context."""
    order = (
        db.query(Order)
        .filter(Order.id == order_id, Order.user_id == current_user.id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail={"error_code": "order_not_found", "message": "Order not found"})
    if order.status == "paid":
        raise HTTPException(status_code=400, detail={"error_code": "already_paid", "message": "Order is already paid"})
    if not order.payment_details_json:
        raise HTTPException(
            status_code=400,
            detail={"error_code": "no_payment_details", "message": "No payment details on file for this order yet"},
        )

    payment_details = json.loads(order.payment_details_json)
    bank_name = payment_details.get("bank_name")
    account_number = payment_details.get("account_number")
    amount = payment_details.get("amount_ngn") or float(order.total_ngn)

    if not bank_name or not account_number:
        raise HTTPException(
            status_code=400,
            detail={"error_code": "incomplete_payment_details", "message": "Payment details are incomplete for this order"},
        )

    try:
        access_token = get_valid_access_token(current_user.id, db)
    except NoMoneiConnection:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "not_connected", "message": "Monei wallet not connected"},
        )
    except MoneiTokenError:
        raise HTTPException(
            status_code=502,
            detail={"error_code": "session_expired", "message": "Could not refresh Monei session, try reconnecting"},
        )

    try:
        banks = get_supported_banks(access_token)
    except MoneiAPIError:
        raise HTTPException(
            status_code=502,
            detail={"error_code": "banks_unavailable", "message": "Could not fetch supported banks from Monei"},
        )

    bank_code = resolve_bank_code(bank_name, banks)
    if not bank_code:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "unknown_bank",
                "message": f"Could not match bank '{bank_name}' to a known Monei bank code",
            },
        )

    try:
        result = send_bank_transfer_payout(
            access_token=access_token,
            amount=amount,
            bank_code=bank_code,
            account_number=account_number,
            transaction_pin=payload.transaction_pin,
            reference=order.order_identifier or order.id,
            narration=f"Jay's Diner order {order.order_identifier or order.id}",
        )
    except MoneiAPIError as e:
        message = str(e)
        raise HTTPException(
            status_code=402,
            detail={"error_code": classify_payout_error(message), "message": message},
        )

    result_status = result.get("status")
    if result_status in ("SUCCESS", "COMPLETED"):
        order.status = "paid"
    elif result_status == "PENDING":
        order.status = "payment_pending"
    else:
        order.status = "payment_failed"
    order.monei_transaction_ref = result.get("reference")
    db.commit()

    return {
        "status": order.status,
        "reference": result.get("reference"),
        "amount": result.get("amount"),
    }