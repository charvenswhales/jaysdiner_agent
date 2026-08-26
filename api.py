"""
FastAPI app exposing the Jay's Diner ordering agent as a simple chat API.
Login required, register/login via /auth, then send the JWT as
"Authorization: Bearer <token>" on /chat and /chat/stream. Each
conversation is scoped to the authenticated user server-side, so a
conversation_id from one user can never be reused to read another user's
chat history, even by guessing.

Run locally:
    uvicorn api:app --reload --port 8000

Then:
    curl -X POST http://localhost:8000/chat \
      -H "Authorization: Bearer <token from /auth/login>" \
      -H "Content-Type: application/json" \
      -d '{"message": "what do you have on the menu?"}'

The response includes a conversation_id, send that back in the next
request's body to continue the same conversation.
"""

import json
import os
import uuid

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session

from agent import run_agent_turn, stream_agent_turn, get_last_transfer_details
from auth.routes import router as auth_router
from auth.dependencies import get_current_user
from db.base import get_db
from db.models import User, CheckoutProfile, Order
from profiles.routes import router as profiles_router
from orders.routes import router as orders_router
from connect.routes import router as connect_router

app = FastAPI(title="Jay's Diner Ordering Agent")

# ALLOWED_ORIGINS is a comma-separated list of real frontend origins
# (e.g. "https://your-app.up.railway.app,http://localhost:3000"). Falls
# back to "*" only if it's not set at all, so local dev keeps working
# without extra config. Auth is still enforced per-request via the JWT
# regardless, this is defense in depth, not the actual access control.
_allowed_origins_env = os.environ.get("ALLOWED_ORIGINS")
_allowed_origins = (
    [o.strip() for o in _allowed_origins_env.split(",")] if _allowed_origins_env else ["*"]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(profiles_router)
app.include_router(orders_router)
app.include_router(connect_router)


class ChatRequest(BaseModel):
    conversation_id: Optional[str] = None
    message: str


class ChatResponse(BaseModel):
    conversation_id: str
    reply: str


def _scoped_thread_id(user: User, conversation_id: str) -> str:
    """The actual LangGraph checkpointer key, always prefixed with the
    AUTHENTICATED user's own ID (never a client-supplied one), so no
    conversation_id a client sends can ever resolve to someone else's
    conversation state."""
    return f"user:{user.id}:{conversation_id}"


def _default_checkout_context(user: User, db: Session) -> Optional[dict]:
    """The logged-in user's default saved checkout profile, shaped for
    injection into the agent's context. None if they haven't saved one."""
    profile = (
        db.query(CheckoutProfile)
        .filter(CheckoutProfile.user_id == user.id, CheckoutProfile.is_default == True)  # noqa: E712
        .first()
    )
    if not profile:
        return None
    return {
        "full_name": profile.full_name,
        "phone": profile.phone,
        "email": user.email,
        "address": profile.address,
        "area": profile.area,
        "delivery_or_pickup": profile.delivery_or_pickup,
    }


@app.post("/chat", response_model=ChatResponse)
def chat(
    req: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conversation_id = req.conversation_id or str(uuid.uuid4())
    thread_id = _scoped_thread_id(current_user, conversation_id)
    checkout_context = _default_checkout_context(current_user, db)
    reply = run_agent_turn(thread_id, req.message, checkout_context)
    return ChatResponse(conversation_id=conversation_id, reply=reply)


@app.post("/chat/stream")
async def chat_stream(
    req: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Server-Sent Events stream for one conversation turn. See
    agent.stream_agent_turn's docstring for the event protocol, tool_start/
    tool_end events let the frontend show live progress and render custom
    UI (e.g. a menu grid) from structured tool output, and text_delta
    events stream the final reply token by token."""
    conversation_id = req.conversation_id or str(uuid.uuid4())
    thread_id = _scoped_thread_id(current_user, conversation_id)
    checkout_context = _default_checkout_context(current_user, db)

    async def event_generator():
        # Peek at each SSE event as it passes through, when place_order_tool
        # actually succeeds, persist it as an Order row, then attach
        # payment/transfer details if get_transfer_details_tool succeeds
        # right after (same turn). agent.py stays unaware of persistence
        # entirely, it just knows how to run a conversation turn.
        pending_checkout_info: Optional[dict] = None
        last_order_db_id: Optional[str] = None

        async for chunk in stream_agent_turn(thread_id, conversation_id, req.message, checkout_context):
            if chunk.startswith("data: "):
                try:
                    event = json.loads(chunk[len("data: "):].strip())
                except json.JSONDecodeError:
                    event = None

                if event and event.get("type") == "tool_start" and event.get("tool") == "place_order_tool":
                    args = event.get("args") or {}
                    pending_checkout_info = {
                        k: args.get(k)
                        for k in ("full_name", "phone", "email", "address", "area", "delivery_or_pickup", "notes")
                    }

                elif event and event.get("type") == "tool_end" and event.get("tool") == "place_order_tool":
                    output = event.get("output") or {}
                    if output.get("status") == "placed":
                        new_order = Order(
                            user_id=current_user.id,
                            order_identifier=output.get("order_identifier"),
                            items_json=json.dumps(output.get("items") or []),
                            checkout_info_json=json.dumps(pending_checkout_info) if pending_checkout_info else None,
                            total_ngn=output.get("total_ngn") or 0,
                            status="placed",
                        )
                        db.add(new_order)
                        db.commit()
                        db.refresh(new_order)
                        last_order_db_id = new_order.id
                    pending_checkout_info = None

                elif event and event.get("type") == "tool_end" and event.get("tool") == "get_transfer_details_tool":
                    output = event.get("output") or {}
                    if output.get("status") == "success":
                        # The tool's own output to the model deliberately
                        # withholds bank_name/account_number (see agent.py) —
                        # get the real values via the backend-only accessor
                        # instead, which never touched agent/LLM context.
                        real_details = get_last_transfer_details(thread_id)
                        reference = output.get("reference")
                        order_row = None
                        if reference:
                            order_row = (
                                db.query(Order)
                                .filter(Order.user_id == current_user.id, Order.order_identifier == reference)
                                .order_by(Order.created_at.desc())
                                .first()
                            )
                        if not order_row and last_order_db_id:
                            order_row = db.query(Order).filter(Order.id == last_order_db_id).first()

                        if order_row and real_details:
                            order_row.payment_details_json = json.dumps(real_details)
                            db.commit()
                            # The frontend needs OUR order id (not Jay's
                            # Diner's order_identifier, which is all the
                            # tool itself knows) to call the pay-order
                            # endpoint, inject it before this goes out.
                            # Every other event passes through unmodified.
                            output["order_id"] = order_row.id
                            event["output"] = output
                            chunk = f"data: {json.dumps(event)}\n\n"

            yield chunk

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if deployed behind it
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/store-status")
def store_status():
    """Public, no login needed. Pure time-of-day computation, no network
    call to Jay's Diner required, so this is safe to call proactively on
    page load rather than waiting for someone to actually ask the agent
    about the menu."""
    from browser_tools import get_store_hours_status

    is_open, note = get_store_hours_status()
    return {"accepting_orders": is_open, "status_note": note}