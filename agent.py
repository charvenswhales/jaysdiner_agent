"""
The conversational agent — wraps browser_tools.py functions as LangChain
tools, uses gpt-4o-mini via OpenRouter for the actual conversation, and
persists state per conversation_id via LangGraph's checkpointer.

Ordering flow through tools:
  get_full_menu / check_items_available   -> read-only, safe to call freely
  check_item_options                       -> read-only, safe to call freely
  add_items_to_cart_tool                   -> mutates a cart, but nothing is
                                               charged yet
  place_order_tool                         -> places a REAL order with a
                                               pending payment. Requires an
                                               explicit confirmation flag —
                                               see its docstring.

NOTE ON MONEY SAFETY: place_order_tool refuses to run unless
user_confirmed_final_total=True is passed, and its own return value (when
refused) instructs the model to summarize and ask the user first. This is
a pragmatic single-process guard, not a hard LangGraph interrupt — worth
hardening into a real human-in-the-loop interrupt before this handles
real customer traffic at scale.
"""

import json
from typing import Optional

from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, BROWSER_AGENT_MODEL
from db.base import DATABASE_URL
from browser_tools import (
    get_menu_inventory,
    match_requested_items,
    check_order_options,
    add_items_to_cart as _add_items_to_cart,
    place_order as _place_order,
    get_paystack_transfer_details as _get_paystack_transfer_details,
)
from schemas import CartItem, CheckoutInfo, PlaceOrderResult, TransferDetails


def _format_menu(inv) -> str:
    by_cat = {}
    for item in inv.items:
        by_cat.setdefault(item.category or "Other", []).append(item)
    lines = []
    for cat, items in by_cat.items():
        lines.append(f"\n{cat}:")
        for i in items:
            lines.append(f"  - {i.name}: NGN{i.price_ngn:,.0f}")
    return "\n".join(lines)


@tool
def get_full_menu() -> str:
    """Get Jay's Diner's full menu with prices, categories, and image URLs,
    plus whether the store is currently accepting orders. Use this whenever
    the user asks what's available, wants to browse, or you need current
    prices. Returns JSON — summarize it naturally for the user, don't dump
    raw JSON in your reply."""
    inv = get_menu_inventory()
    payload = {
        "store_accepting_orders": inv.store_accepting_orders,
        "store_status_note": inv.store_status_note,
        "items": [
            {
                "name": i.name,
                "price_ngn": i.price_ngn,
                "category": i.category,
                "image_url": i.image_url,
            }
            for i in inv.items
        ],
    }
    return json.dumps(payload)


@tool
def check_items_available(item_names: list[str]) -> str:
    """Check whether specific requested items exist on the menu, with fuzzy
    matching (e.g. 'burger' matches 'Nashville Burger'). Use this when the
    user names specific items they want, to confirm exact names and prices
    before adding to cart. Returns JSON — summarize it naturally for the
    user, don't dump raw JSON in your reply."""
    inv = get_menu_inventory()
    matches = match_requested_items(item_names, inv)
    by_name = {i.name: i for i in inv.items}
    results = []
    for m in matches.matches:
        entry = {
            "requested_name": m.requested_name,
            "matched": m.matched,
            "matched_name": m.matched_name,
            "price_ngn": m.price_ngn,
        }
        if m.matched and m.matched_name in by_name:
            entry["image_url"] = by_name[m.matched_name].image_url
        results.append(entry)
    return json.dumps({"matches": results})


@tool
def check_item_options(item_names: list[str]) -> str:
    """Check what customization options (spice level, sauce, side choices,
    etc.) specific menu items have, and which are required vs optional.
    item_names must be the EXACT menu names — use check_items_available
    first if you're not sure of the exact spelling. Call this before adding
    any item that might have required choices. Returns JSON — summarize it
    naturally for the user, don't dump raw JSON in your reply."""
    inv = get_menu_inventory()
    result = check_order_options(inv, item_names)
    return json.dumps(result.model_dump())


@tool
def add_items_to_cart_tool(items_json: str) -> str:
    """Add items to the cart. items_json must be a JSON array like:
    [{"name": "BUFFALO TENDERS", "quantity": 1, "option_preferences": {}},
     {"name": "SHRIMP ISLAND", "quantity": 1, "option_preferences": {"Choose Sauce": ["Pink Sauce"]}}]
    Use exact menu names (from check_items_available). option_preferences
    maps an option group name to the chosen label(s) — omit a group to get
    a sensible automatic default (cheapest choice if required, untouched
    if optional). This does NOT charge anything — it just stages the cart.
    Returns JSON — summarize it naturally for the user, don't dump raw JSON
    in your reply."""
    inv = get_menu_inventory()
    raw_items = json.loads(items_json)
    cart_items = [CartItem(**i) for i in raw_items]
    results = _add_items_to_cart(inv, cart_items)
    total = sum(r.line_price_ngn or 0 for r in results if r.success)
    return json.dumps({
        "results": [r.model_dump() for r in results],
        "running_total_ngn": total,
    })


@tool
def place_order_tool(
    items_json: str,
    full_name: str,
    phone: str,
    email: str,
    delivery_or_pickup: str,
    address: str = "",
    area: str = "",
    notes: str = "",
    user_confirmed_final_total: bool = False,
    config: RunnableConfig = None,
) -> str:
    """Place the REAL order — this creates an actual pending order that
    will need payment. items_json is the same format as
    add_items_to_cart_tool. delivery_or_pickup must be "delivery" or
    "takeaway". area is required for delivery (must be one of: Chevron,
    Osapa, Ikate, Lekki Phase 1, Victoria Island, Ikoyi).

    CRITICAL: you MUST set user_confirmed_final_total=True only if the
    user has just explicitly confirmed (in their most recent message)
    that they want to place the order, AFTER you have shown them the
    exact items and total price. If you call this with
    user_confirmed_final_total=False (or omit it), this tool will NOT
    place the order — it will instead tell you to summarize the order
    and total and ask the user to confirm first."""
    if not user_confirmed_final_total:
        return json.dumps({
            "status": "needs_confirmation",
            "message": (
                "Do not place this order yet. Show the user the exact items, "
                "any required option choices, and the total price (including "
                "delivery fee if applicable), and ask them to explicitly "
                "confirm before calling this tool again with "
                "user_confirmed_final_total=True."
            ),
        })

    inv = get_menu_inventory()
    raw_items = json.loads(items_json)
    cart_items = [CartItem(**i) for i in raw_items]
    checkout = CheckoutInfo(
        full_name=full_name,
        phone=phone,
        email=email,
        address=address,
        delivery_or_pickup=delivery_or_pickup,
        area=area or None,
        notes=notes or None,
    )
    cart_results, order_result = _place_order(inv, cart_items, checkout)
    if not order_result.success:
        return json.dumps({"status": "failed", "message": order_result.notes})

    # Remembered here (not sent back to the model) so get_transfer_details_tool
    # can read it directly — keeps the Paystack public key and customer email
    # out of the model's context, and avoids relying on the model to
    # correctly carry those values forward into the next tool call.
    # Keyed by thread_id (injected automatically by LangGraph via
    # RunnableConfig, never exposed to the model's tool-calling schema) so
    # concurrent conversations from different users never clobber each
    # other's order state — this used to be a single bare global, which
    # would have leaked one customer's order into another's payment flow
    # the moment two people used this at the same time.
    thread_id = (config or {}).get("configurable", {}).get("thread_id", "unknown")
    _last_order_results[thread_id] = order_result

    # Enrich each ordered item with its image + actual line price for the
    # order-history detail view — cart_results already has the price, and
    # inventory has the image, neither of which the model itself needs to
    # see, but the frontend does.
    inventory_by_name = {i.name: i for i in inv.items}
    enriched_items = []
    for r in cart_results:
        menu_item = inventory_by_name.get(r.item_name)
        enriched_items.append({
            "name": r.item_name,
            "success": r.success,
            "line_price_ngn": r.line_price_ngn,
            "notes": r.notes,
            "image_url": menu_item.image_url if menu_item else None,
        })

    return json.dumps({
        "status": "placed",
        "order_identifier": order_result.order_identifier,
        "total_ngn": order_result.total_ngn,
        "payment_method": order_result.payment_method,
        "items": enriched_items,
    })


# Keyed by thread_id, not a bare global — see the comment inside
# place_order_tool above for why this matters.
_last_order_results: dict[str, PlaceOrderResult] = {}


@tool
def get_transfer_details_tool(config: RunnableConfig = None) -> str:
    """After an order has just been placed successfully (place_order_tool
    returned status "placed"), call this with no arguments to prepare
    payment behind the scenes. This can take a few seconds since it
    involves loading the payment gateway. Returns JSON with the amount and
    a reference — payment now happens entirely through a Monei payment
    card the interface shows the customer; you do not need or receive any
    bank/account details, and must never describe a manual bank transfer."""
    thread_id = (config or {}).get("configurable", {}).get("thread_id", "unknown")
    last_order_result = _last_order_results.get(thread_id)

    if last_order_result is None or not last_order_result.success:
        return json.dumps({
            "status": "error",
            "message": "No successfully placed order found. Place an order first.",
        })
    try:
        transfer = _get_paystack_transfer_details(last_order_result)
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Could not retrieve transfer details: {e}"})

    # bank_name/account_number are deliberately withheld from what the
    # model sees, same reasoning as the Paystack public key/customer email
    # above — a model that can see an account number will eventually
    # recite it regardless of what the system prompt says not to do.
    # api.py reads the real values via get_last_transfer_details() instead,
    # which never touches agent/LLM context. Also keyed by thread_id now,
    # same concurrency reasoning as _last_order_results above.
    _last_transfer_details_by_thread[thread_id] = transfer

    return json.dumps({
        "status": "success",
        "amount_ngn": transfer.amount_ngn,
        "reference": transfer.reference,
    })


_last_transfer_details_by_thread: dict[str, TransferDetails] = {}


def get_last_transfer_details(thread_id: str) -> Optional[dict]:
    """Backend-only accessor — NOT a tool, never exposed to the model.
    api.py calls this (with the SAME thread_id it used for this
    conversation turn) to get the real bank_name/account_number needed to
    actually execute the Monei payout, without those values ever having
    existed in the model's context."""
    details = _last_transfer_details_by_thread.get(thread_id)
    if details is None:
        return None
    return {
        "bank_name": details.bank_name,
        "account_number": details.account_number,
        "amount_ngn": details.amount_ngn,
        "reference": details.reference,
    }


TOOLS = [
    get_full_menu,
    check_items_available,
    check_item_options,
    add_items_to_cart_tool,
    place_order_tool,
    get_transfer_details_tool,
]

SYSTEM_PROMPT = """You are the ordering assistant for Jay's Diner, a restaurant in Ikoyi, Lagos.
Help customers browse the menu, answer questions about items, and place orders.

Rules:
- Always use the tools to check real menu data, prices, and availability. Never guess or invent items or prices, and never answer from memory of earlier in this conversation. Call the relevant tool again every time, even if you already showed the same information earlier, since prices and availability can change and the interface needs the tool's response to render item cards.
- When get_full_menu or check_items_available returns items, the interface already shows those items as visual cards with photos and prices right there on screen. Do not list the items and prices again in your reply. Just give a short, natural acknowledgment instead, for example "Here's what's on the menu tonight" or "Here's what we've got for that." If the user asks a specific question about an item (like what's in it, or how spicy it is), answer that question directly instead of repeating the whole list.
- Markdown formatting (bold, bullet lists, headings) is fine and gets rendered nicely when you do need to write out details, just not for a plain menu listing that the cards already cover.
- Never include image links or URLs in your text. The interface already shows item photos as visual cards alongside your reply.
- Tool results come back as JSON. Read them, then reply in natural conversational language. Never paste raw JSON into your reply to the user.
- When an item has required options (check with check_item_options), ask the user for their preference before adding to cart, unless they've already stated it.
- Before calling place_order_tool for real, always show the user a clear summary of items and total price and get explicit confirmation first.
- If a system message with saved checkout details is present, treat it as the default. Don't ask the user to repeat their name, phone, or address unless they want to change it or use a different address for this order.
- Immediately after place_order_tool returns status "placed", call get_transfer_details_tool with no arguments. This prepares payment behind the scenes and can take a few seconds, that's normal. Once it succeeds, just tell the user their order is confirmed and ready for payment. The interface shows a secure Monei payment card for them to complete it. NEVER mention bank names, account numbers, or ask them to make a manual transfer. Payment happens entirely through the Monei card shown in the interface, not through anything you say.
- Keep responses conversational and concise. This is a chat interface, not a report.
- If the store isn't currently accepting orders, let the user know early.
"""


def _llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=BROWSER_AGENT_MODEL,
        base_url=OPENROUTER_BASE_URL,
        api_key=OPENROUTER_API_KEY,
        max_completion_tokens=2048,
        timeout=60,
        # Automatically retries transient failures (dropped connections,
        # rate limits, brief network blips) a few times before giving up,
        # rather than surfacing the very first hiccup straight to the
        # user as a broken conversation.
        max_retries=3,
    )


import asyncio

_agent_instance = None
_agent_lock = asyncio.Lock()
_checkpointer_pool = None  # kept open for the app's whole lifetime


async def _get_agent():
    """Builds the compiled agent once, on first real use, and reuses it
    after that. This has to happen lazily rather than at plain module
    import time, because the Postgres checkpointer needs an async
    connection that's tied to whatever event loop creates it.

    Uses a connection pool rather than a single raw connection. The first
    version of this held one connection open for the app's entire
    lifetime, which worked for a while and then started failing with
    "the connection is closed" on both localhost and Railway. Neon is
    serverless Postgres and closes idle connections when it suspends, so
    a single long-lived connection is guaranteed to die eventually. A
    pool replaces a dead connection with a fresh one automatically
    instead of getting stuck reusing one that no longer works."""
    global _agent_instance, _checkpointer_pool

    if _agent_instance is not None:
        return _agent_instance

    async with _agent_lock:
        if _agent_instance is not None:
            return _agent_instance

        if DATABASE_URL.startswith("postgres"):
            from psycopg_pool import AsyncConnectionPool
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            _checkpointer_pool = AsyncConnectionPool(
                conninfo=DATABASE_URL,
                max_size=10,
                kwargs={"autocommit": True, "prepare_threshold": 0},
                # Checks a connection is actually alive right before
                # handing it out, the same idea as SQLAlchemy's
                # pool_pre_ping on the other connection. Neon can close a
                # connection at any moment when it suspends, this doesn't
                # stop that from happening, nothing can, but it catches
                # more of the dead ones at borrow time instead of only
                # discovering they're dead mid-query.
                check=AsyncConnectionPool.check_connection,
                open=False,
            )
            await _checkpointer_pool.open()
            checkpointer = AsyncPostgresSaver(_checkpointer_pool)
            await checkpointer.setup()  # idempotent, creates the checkpoint tables if they don't exist yet
        else:
            checkpointer = MemorySaver()

        _agent_instance = create_react_agent(_llm(), TOOLS, prompt=SYSTEM_PROMPT, checkpointer=checkpointer)
        return _agent_instance


def _build_messages(user_message: str, checkout_context: Optional[dict]) -> list[dict]:
    """Prepend a system message with the logged-in user's saved checkout
    info, if any, so the model can use it without asking every time. Sent
    every turn (not just the first) — small enough in size that the
    context-bloat cost is worth not having to track "is this a new
    conversation" separately."""
    messages = []
    if checkout_context:
        messages.append({
            "role": "system",
            "content": (
                "The logged-in user has a saved checkout profile — use these "
                "details by default when placing an order instead of asking "
                "for them again, but confirm them with the user first and let "
                "them override anything they want to change: "
                + json.dumps(checkout_context)
            ),
        })
    messages.append({"role": "user", "content": user_message})
    return messages


def run_agent_turn(thread_id: str, user_message: str, checkout_context: Optional[dict] = None) -> str:
    """Run one turn of the conversation. LangGraph's checkpointer keeps
    the full message history keyed by thread_id, so each call only needs
    the new user message. thread_id should already be scoped to the
    authenticated user (see api.py) — this function doesn't enforce that
    itself, it just uses whatever key it's given.

    Note: the actual deployed frontend only ever calls stream_agent_turn
    below, via /chat/stream. This sync path isn't wired up to anything
    real right now, and doesn't correctly support the async Postgres
    checkpointer, it would hit the same event-loop mismatch that used to
    break streaming chat in production. Fine for local MemorySaver
    testing, would need converting to async before it could safely back
    a real Postgres-backed deployment."""
    agent = asyncio.run(_get_agent())
    config = {"configurable": {"thread_id": thread_id}}
    result = agent.invoke(
        {"messages": _build_messages(user_message, checkout_context)},
        config=config,
    )
    last_message = result["messages"][-1]
    return last_message.content

# ---------------------------------------------------------------------------
# Streaming — SSE event protocol
# ---------------------------------------------------------------------------
#
# Event shapes, one JSON object per SSE "data:" line:
#   {"type": "start", "conversation_id": "..."}
#   {"type": "tool_start", "tool": "get_full_menu", "args": {...}}
#   {"type": "tool_end", "tool": "get_full_menu", "output": {...} | "raw text"}
#   {"type": "text_delta", "content": "..."}      -- streamed token chunks
#   {"type": "done"}
#   {"type": "error", "message": "..."}
#
# "output" on tool_end is JSON-parsed when the tool returned JSON (which is
# every tool now) — that's the payload a frontend uses to render custom UI
# (e.g. a menu grid with images) for that specific tool. Fall back to the
# raw string if a tool ever returns plain text.


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _try_parse_json(text):
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


async def stream_agent_turn(
    thread_id: str,
    conversation_id: str,
    user_message: str,
    checkout_context: Optional[dict] = None,
):
    """Async generator yielding SSE-formatted strings for one turn.

    thread_id: the LangGraph checkpointer key — should be scoped to the
    authenticated user (e.g. "user:<user_id>:<conversation_id>") so one
    user can never read another user's conversation state, even by
    guessing or reusing a conversation_id. See api.py.

    conversation_id: the plain, client-facing ID echoed back in the
    "start" event — deliberately NOT the same as thread_id, so the
    user-scoping prefix never leaks to the client.

    checkout_context: the logged-in user's saved default checkout
    profile (if any), injected as context every turn — see
    _build_messages."""
    config = {"configurable": {"thread_id": thread_id}}
    yield _sse({"type": "start", "conversation_id": conversation_id})

    # A dropped connection can still happen occasionally even with a
    # pool in front of it, Neon can close a connection at the exact
    # moment it's mid-use, not just while idle. When that happens right
    # at the start of a turn (loading the conversation's prior state,
    # before any tool has run) it's completely safe to quietly retry
    # with a fresh connection, since nothing has happened yet. Once a
    # single tool_start or piece of the reply has already streamed to
    # the user, retrying is no longer safe, a tool might have already
    # run, so from that point on a failure is just shown, not retried.
    max_attempts = 2
    any_event_emitted = False

    for attempt in range(1, max_attempts + 1):
        try:
            agent = await _get_agent()
            async for event in agent.astream_events(
                {"messages": _build_messages(user_message, checkout_context)},
                config=config,
                version="v2",
            ):
                kind = event["event"]

                if kind == "on_tool_start":
                    any_event_emitted = True
                    yield _sse({
                        "type": "tool_start",
                        "tool": event["name"],
                        "args": event["data"].get("input"),
                    })

                elif kind == "on_tool_end":
                    any_event_emitted = True
                    raw_output = event["data"].get("output")
                    content = getattr(raw_output, "content", raw_output)
                    parsed = _try_parse_json(content)
                    yield _sse({
                        "type": "tool_end",
                        "tool": event["name"],
                        "output": parsed if parsed is not None else content,
                    })

                elif kind == "on_chat_model_stream":
                    chunk = event["data"].get("chunk")
                    text = getattr(chunk, "content", "") if chunk is not None else ""
                    if text:
                        any_event_emitted = True
                        yield _sse({"type": "text_delta", "content": text})

            break  # completed without error, nothing left to retry

        except Exception as e:
            message = str(e)
            is_connection_error = any(
                s in message
                for s in (
                    "SSL connection",
                    "consuming input failed",
                    "connection is closed",
                    "connection already closed",
                )
            )

            if is_connection_error and not any_event_emitted and attempt < max_attempts:
                continue  # quiet retry, nothing has reached the user yet

            # A dangling tool call with no matching result means an
            # earlier turn in THIS conversation got interrupted
            # mid-flight and left a broken checkpoint behind. The
            # conversation can't continue from that point, ever, no
            # amount of retrying the next message fixes it. Say so
            # plainly instead of surfacing LangGraph's internal
            # validation error.
            if "corresponding ToolMessage" in message or "INVALID_CHAT_HISTORY" in message:
                message = (
                    "This conversation hit an error partway through an earlier "
                    "step and can't continue. Please start a new chat, sorry "
                    "about that."
                )
            elif is_connection_error:
                message = "Lost connection partway through that, please try again."

            yield _sse({"type": "error", "message": message})
            break

    yield _sse({"type": "done"})