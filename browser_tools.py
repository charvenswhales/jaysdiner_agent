"""
Tool functions for the Jay's Diner ordering agent, split by cost/purpose:

  get_menu_inventory()      - plain HTTP GET + HTML parsing. NOT a browser
                               agent at all, the menu is fully server-
                               rendered, so this is instant, free, and
                               deterministic. Cacheable.
  match_requested_items()   - plain LLM call, NOT a browser agent. Cheap.
  check_order_options()     - browser-use, opens modals for SPECIFIC matched items
                               only, reads option groups, never submits.
  build_order_and_read_transfer_details()
                             - browser-use, the only function that actually adds
                               to cart / submits checkout. Stops before payment.

NOTE: browser-use ships fast, double check `output_model_schema` and the
ChatOpenAI wrapper kwargs against https://docs.browser-use.com if things
start erroring on a version bump.
"""

import json
import re
import http.server
import socketserver
import threading
import tempfile
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from browser_use import Agent, ChatOpenAI
from openai import OpenAI
from playwright.sync_api import sync_playwright

from config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, BROWSER_AGENT_MODEL, SITE_URL, HEADLESS
from schemas import (
    MenuItem,
    MenuInventoryResult,
    MatchResult,
    MatchedItem,
    ItemOptions,
    CheckOrderOptionsResult,
    OrderResult,
    CartItem,
    CheckoutInfo,
    AddToCartResult,
    PlaceOrderResult,
    TransferDetails,
)


def _llm() -> ChatOpenAI:
    """OpenRouter-backed LLM for browser-use agents.

    max_completion_tokens is raised above the 4096 default, full-menu
    scans and similar large structured outputs (e.g. ~50 menu items) can
    exceed 4096 tokens once schema field names are repeated per item,
    causing silent truncation (a malformed/empty final item) rather than
    an error.

    timeout is raised above browser-use's ~75s default, larger completions
    naturally take longer to generate, and OpenRouter's routing for a cheap
    model can add its own latency, so the default was tripping even on
    otherwise-successful calls.
    """
    return ChatOpenAI(
        model=BROWSER_AGENT_MODEL,
        base_url=OPENROUTER_BASE_URL,
        api_key=OPENROUTER_API_KEY,
        max_completion_tokens=8192,
        timeout=180,
    )


def _plain_client() -> OpenAI:
    """Plain OpenRouter chat client for non-browser calls (cheap, single-shot)."""
    return OpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)


CAROUSEL_WARNING = """
IMPORTANT, avoid the carousel:
The homepage has a "Best Sellers" carousel near the top that duplicates
items multiple times (cloned slides for infinite scroll). Do NOT interact
with anything inside that carousel. Scroll down past it to the full
"Main Menu" section, organized into categories (Tenders, Crispy Chick,
Hotdogs, Wings, Pasta, Combo Meals, Sides, Desserts, Milkshakes & Drinks,
Sauces), always work within that section instead.
""".strip()


# ---------------------------------------------------------------------------
# Phase 1, menu inventory (plain HTTP GET + HTML parsing, no browser/LLM)
# ---------------------------------------------------------------------------
#
# The menu is fully server-rendered, every category, item name, price, and
# product ID is present in the raw HTML on first load, no JS execution
# needed to see it. So this doesn't touch browser-use at all: it's a plain
# GET + BeautifulSoup parse, matching the DOM structure:
#
#   <div class="category" id="...">
#     <div class="sub-title"><h3><span>CATEGORY NAME</span></h3></div>
#     <div class="products">...
#       <div class="box-product">...
#         <div class="title mob-title">ITEM NAME</div>
#         <div class="price">₦ 8,500</div>
#         <a class="show-modal" data-product="79">Add to cart</a>
#
# The order-suspension notice was previously read from:
#   <div id="allowOrderModal">...<div class="modal-body"><p>...</p></div>
# but that turned out to be unreliable, the note can linger in the page's
# markup even when the store is actually open. Store hours are fixed and
# known (12pm–5am daily, Lagos time), so we check the time instead of
# scraping a banner that doesn't reliably reflect current status.
#
# If the site's markup changes, this will need updating, but it's still
# far cheaper/faster/more reliable than an LLM agent for a static page.

def _clean_price(text: str) -> float:
    """'₦\n 8,500' -> 8500.0"""
    digits = re.sub(r"[^\d.]", "", text)
    return float(digits) if digits else 0.0


LAGOS_TZ = ZoneInfo("Africa/Lagos")


def _store_hours_status() -> tuple[bool, Optional[str]]:
    """Jay's Diner is open daily 12pm (noon) to 5am the next day, Lagos time."""
    now = datetime.now(LAGOS_TZ)
    is_open = now.hour >= 12 or now.hour < 5
    if is_open:
        return True, None
    return False, (
        f"Jay's Diner is closed right now ({now.strftime('%I:%M %p')} WAT). "
        "Open daily 12pm–5am."
    )


def _extract_product_image(product_div) -> Optional[str]:
    """Product cards put a small badge icon (chili-spice marker, "new"
    badge) BEFORE the real product photo inside .image, e.g.:
      <img class="md-chili-icon" src=".../images/new_chilli.png">
      <img src="https://s3.../storage.jaysdinerng.com/.../thumb.webp">
    Badge icons are always served from jaysdinerng.com/images/, real
    product photos always come from the S3 storage bucket, use that to
    pick the right one instead of just taking the first <img>."""
    candidates = product_div.select(".image img")
    for img in candidates:
        src = img.get("src", "")
        if "storage.jaysdinerng.com" in src:
            return src
    return candidates[-1].get("src") if candidates else None


def get_menu_inventory() -> MenuInventoryResult:
    response = httpx.get(SITE_URL, timeout=20, follow_redirects=True)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    store_accepting_orders, store_status_note = _store_hours_status()

    # --- menu items ---
    items: list[MenuItem] = []
    for category_div in soup.select("div.category"):
        category_name_el = category_div.select_one(".sub-title h3 span")
        category_name = category_name_el.get_text(strip=True) if category_name_el else None

        for product_div in category_div.select(".box-product"):
            title_el = product_div.select_one(".title.mob-title")
            price_el = product_div.select_one(".price")
            link_el = product_div.select_one("a.show-modal")

            if not title_el or not price_el:
                continue

            product_id = link_el.get("data-product") if link_el else None
            image_url = _extract_product_image(product_div)
            items.append(MenuItem(
                name=title_el.get_text(strip=True),
                price_ngn=_clean_price(price_el.get_text()),
                category=category_name,
                product_id=product_id,
                image_url=image_url,
            ))

    return MenuInventoryResult(
        store_accepting_orders=store_accepting_orders,
        store_status_note=store_status_note,
        items=items,
    )
# ---------------------------------------------------------------------------

def match_requested_items(requested_items: list[str], inventory: MenuInventoryResult) -> MatchResult:
    menu_text = "\n".join(f"- {i.name}, ₦{i.price_ngn:,.0f} ({i.category or 'uncategorized'})" for i in inventory.items)

    prompt = f"""
You are matching a customer's requested items against a restaurant's menu.

MENU:
{menu_text}

CUSTOMER REQUESTED:
{", ".join(requested_items)}

For each requested item, find the closest real menu match (loose matching is
fine, e.g. "burger" -> "Nashville Burger" if that's the best fit). If there's
no reasonable match, mark it unmatched, do not invent an item, and leave
matched_name and price_ngn as null (not 0) when unmatched.

Respond with ONLY valid JSON matching this exact shape, no other text:
{{
  "matches": [
    {{"requested_name": "...", "matched": true, "matched_name": "...", "price_ngn": 0, "notes": "..."}}
  ]
}}
""".strip()

    client = _plain_client()
    response = client.chat.completions.create(
        model=BROWSER_AGENT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    content = response.choices[0].message.content.strip()
    # Strip markdown code fences if the model added them despite instructions
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    return MatchResult.model_validate(json.loads(content))


# ---------------------------------------------------------------------------
# Phase 3, discover order options for SPECIFIC matched items
# ---------------------------------------------------------------------------
#
# No browser needed here either. The site's own JS handler for ".show-modal"
# does exactly this:
#
#   POST /product/show   body: {_token, product_id}   -> returns modal HTML
#
# We replicate that call directly with httpx (same session, so the CSRF
# token lines up), get the modal fragment back in milliseconds, then hand
# just the options portion of that fragment to ONE plain LLM call per item
# to classify the option groups (required vs optional, choices) per our
# rules. No agent loop, no clicking, no stale DOM indices.

def _extract_options_html(modal_html: str) -> str:
    """Strip a modal response down to just its option-groups section —
    keeps the LLM call small and fast."""
    soup = BeautifulSoup(modal_html, "html.parser")
    parts = []
    options_container = soup.select_one(".products-options-container")
    if options_container:
        parts.append(str(options_container))
    upgrade_block = soup.select_one(".upgrade")
    if upgrade_block:
        parts.append(str(upgrade_block))
    return "\n".join(parts) if parts else modal_html


def _get_csrf_token(http: httpx.Client) -> str:
    home = http.get(SITE_URL)
    home.raise_for_status()
    token_el = BeautifulSoup(home.text, "html.parser").select_one('meta[name="csrf-token"]')
    return token_el["content"] if token_el else None


def _fetch_product_modal(http: httpx.Client, csrf_token: str, product_id: str) -> str:
    resp = http.post(
        f"{SITE_URL}/product/show",
        data={"_token": csrf_token, "product_id": product_id},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    resp.raise_for_status()
    return resp.text


def check_order_options(inventory: MenuInventoryResult, matched_item_names: list[str]) -> CheckOrderOptionsResult:
    by_name = {i.name: i for i in inventory.items}
    client = _plain_client()
    results: list[ItemOptions] = []

    with httpx.Client(timeout=20, follow_redirects=True) as http:
        csrf_token = _get_csrf_token(http)

        for name in matched_item_names:
            item = by_name.get(name)
            if not item or not item.product_id:
                results.append(ItemOptions(matched_name=name, option_groups=[]))
                continue

            modal_html = _fetch_product_modal(http, csrf_token, item.product_id)
            options_html = _extract_options_html(modal_html)

            if not options_html.strip():
                results.append(ItemOptions(matched_name=name, option_groups=[]))
                continue

            prompt = f"""
Below is the HTML for a product customization modal's option groups. Classify
each option group per these exact rules:

- A group is REQUIRED if its container div has a "val-required" class, its
  title text has an asterisk (*), or (for "upgrade-option" blocks) its
  data-min attribute is greater than 0. Otherwise it's OPTIONAL.
- If required, min_required is the data-min value (e.g. "Choose Two Side"
  needs 2).
- List each choice's visible label text and any extra price shown next to
  it (many are ₦0, use null if there's no visible price at all).
- A single-selection/multiple-selection/repeat-selections group's name is
  in its "title" span text. An upgrade-option group's name is in its
  "opt-cat-title" span text.

HTML:
{options_html}

Respond with ONLY valid JSON matching this exact shape, no other text:
{{
  "option_groups": [
    {{"name": "...", "required": true, "min_required": 2, "choices": [{{"label": "...", "extra_price_ngn": 0}}]}}
  ]
}}
If there are no option groups at all, respond {{"option_groups": []}}.
""".strip()

            response = client.chat.completions.create(
                model=BROWSER_AGENT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            content = response.choices[0].message.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            parsed = json.loads(content)
            results.append(ItemOptions(matched_name=name, option_groups=parsed.get("option_groups", [])))

    return CheckOrderOptionsResult(items=results)


# ---------------------------------------------------------------------------
# Phase 4, add items to cart via direct HTTP POST (replicating /cart/store)
# ---------------------------------------------------------------------------
#
# #cart-form is a REAL (non-AJAX) form submit, its own JS only calls
# event.preventDefault() when validation fails, meaning on success the
# browser just POSTs the form normally. That means we can build the exact
# same POST body ourselves from the modal's hidden <input> fields, and skip
# a browser for cart-building entirely too.
#
# For required option groups, we default to the cheapest (ideally free)
# choice per group, since we have no explicit user preference at this
# stage. Optional groups are left completely untouched (their fields
# default to "0"/unselected, matching what a real browser submits when the
# user never interacts with them).

def _option_price(el) -> float:
    price_el = el.select_one("[data-value]")
    if not price_el:
        return 0.0
    try:
        return float(price_el["data-value"])
    except (KeyError, ValueError):
        return 0.0


def _group_title(group) -> str:
    # Prefer the clean data-name attribute (present on single-selection,
    # repeat-selections, and multiple-selection group containers) over
    # parsing the visible title text, which has unreliable whitespace
    # around a trailing asterisk for required groups.
    data_name = group.get("data-name")
    if data_name and data_name.strip():
        return data_name.strip()
    title_el = group.select_one(".title, .opt-cat-title")
    if not title_el:
        return ""
    return title_el.get_text(strip=True).rstrip("*").strip()


def _box_label(box) -> str:
    # Use a DIRECT child selector, not a descendant selector, a choice's
    # <label> often contains a nested, empty checkmark <span> (inside a
    # .check div) BEFORE the actual text <span>, which is a direct label
    # child. "label span" (descendant) grabs the empty one first; "label >
    # span" (direct child) correctly skips past the nested one.
    label_span = box.select_one("label > span")
    if label_span:
        text = label_span.get_text(strip=True)
        if text:
            return text
    rs_span = box.select_one(".rs-text > span")
    if rs_span:
        return rs_span.get_text(strip=True)
    return box.get_text(strip=True)


def _preferred_boxes(boxes, group_name: str, preferences: dict) -> Optional[list]:
    """If the caller specified a preference for this group name, return the
    matching boxes. Returns None if no preference was given for this group
    (caller should fall back to a default), or an empty list if a
    preference was given but nothing matched (caller should NOT silently
    substitute a default in that case, better to surface the mismatch)."""
    wanted = None
    for pref_name, labels in preferences.items():
        if pref_name.strip().lower() == group_name.strip().lower():
            wanted = [l.strip().lower() for l in labels]
            break
    if wanted is None:
        return None
    return [b for b in boxes if _box_label(b).strip().lower() in wanted]


def _select_options(form_soup, fields: dict, preferences: dict) -> tuple[list[str], float]:
    """Mutates `fields` in place to reflect selected options. Returns
    (warnings, extra_cost), extra_cost is the sum of every selected
    option's price, which must be added to the item's base price since the
    modal's static .total-price value never reflects our selections (that
    recalculation normally happens in the site's own client-side JS, which
    we're not running)."""
    warnings: list[str] = []
    extra_cost = 0.0

    # Single-selection groups (radio-style)
    for group in form_soup.select(".single-selection.parent"):
        group_name = _group_title(group)
        required = "val-required" in (group.get("class") or [])
        boxes = group.select(".options .box")
        if not boxes:
            continue

        chosen = _preferred_boxes(boxes, group_name, preferences)
        if chosen is not None and not chosen:
            warnings.append(f"No option matched your preference for '{group_name}', none selected.")
            chosen = None
        if chosen is None:
            if not required:
                continue  # optional, no preference given, leave untouched
            chosen = [min(boxes, key=_option_price)]

        box = chosen[0]
        checkbox = box.select_one('input[type="checkbox"]')
        qty_input = box.select_one('input[type="hidden"].close-inp')
        if checkbox and checkbox.get("name") and qty_input and qty_input.get("name"):
            fields[checkbox["name"]] = checkbox.get("value", "")
            fields[qty_input["name"]] = "1"
            extra_cost += _option_price(box)

    # Repeat-selections groups (stepper-style, pick N)
    for group in form_soup.select(".repeat-selections.parent"):
        group_name = _group_title(group)
        required = "val-required" in (group.get("class") or [])
        try:
            min_required = int((group.get("data-min") or "0").strip())
        except ValueError:
            min_required = 0
        choices = group.select(".repeat-selection")
        if not choices:
            continue

        chosen = _preferred_boxes(choices, group_name, preferences)
        if chosen is not None and len(chosen) < max(min_required, 1):
            warnings.append(
                f"Only matched {len(chosen)} of your preferences for '{group_name}' "
                f"(needs {min_required}), filling the rest with cheapest options."
            )
            remaining_needed = max(min_required, 1) - len(chosen)
            already_chosen_ids = {id(c) for c in chosen}
            leftovers = [c for c in choices if id(c) not in already_chosen_ids]
            chosen += sorted(leftovers, key=_option_price)[:remaining_needed]
        if chosen is None:
            if not required or min_required <= 0:
                continue
            chosen = sorted(choices, key=_option_price)[:min_required]

        for choice in chosen:
            qty_input = choice.select_one('input[type="hidden"].qty-upgrade')
            if qty_input and qty_input.get("name"):
                fields[qty_input["name"]] = "1"
                extra_cost += _option_price(choice)

    # Multiple-selection groups (checkbox add-ons, always optional, only
    # touch if the caller explicitly asked for one)
    for group in form_soup.select(".multiple-selection.parent"):
        group_name = _group_title(group)
        boxes = group.select(".box")
        if not boxes:
            continue
        chosen = _preferred_boxes(boxes, group_name, preferences)
        if not chosen:
            continue
        for box in chosen:
            checkbox = box.select_one('input[type="checkbox"]')
            qty_input = box.select_one('input[type="hidden"].close-inp')
            if checkbox and checkbox.get("name") and qty_input and qty_input.get("name"):
                fields[checkbox["name"]] = checkbox.get("value", "")
                fields[qty_input["name"]] = "1"
                extra_cost += _option_price(box)

    return warnings, extra_cost


def _add_single_item_to_cart(http: httpx.Client, csrf_token: str, inventory: MenuInventoryResult, cart_item: CartItem) -> AddToCartResult:
    by_name = {i.name: i for i in inventory.items}
    menu_item = by_name.get(cart_item.name)
    if not menu_item or not menu_item.product_id:
        return AddToCartResult(
            item_name=cart_item.name, success=False,
            notes="Item not found in inventory or missing product_id",
        )

    modal_html = _fetch_product_modal(http, csrf_token, menu_item.product_id)
    form_soup = BeautifulSoup(modal_html, "html.parser").select_one("#cart-form")
    if not form_soup:
        return AddToCartResult(
            item_name=cart_item.name, success=False,
            notes="Could not find #cart-form in modal response",
        )

    # Default fields: every hidden input as-is. This naturally captures
    # _token, product_id, quantity, price, and every option's default "0"
    # quantity, mirroring what a real browser submits when the user
    # hasn't touched anything.
    fields = {
        inp["name"]: inp.get("value", "")
        for inp in form_soup.select('input[type="hidden"]')
        if inp.get("name")
    }
    fields["quantity"] = str(cart_item.quantity)

    warnings, extra_cost = _select_options(form_soup, fields, cart_item.option_preferences)

    # The modal's static .total-price is the BASE price only, it never
    # reflects our selections (that recalculation normally happens in
    # client-side JS we're not running). So compute the real total
    # ourselves: base + every selected option's price.
    total_price_el = form_soup.select_one(".total-price")
    base_price = 0.0
    if total_price_el and total_price_el.get("data-value"):
        try:
            base_price = float(total_price_el["data-value"])
        except ValueError:
            pass
    final_price = base_price + extra_cost
    fields["price"] = str(final_price)

    store_resp = http.post(f"{SITE_URL}/cart/store", data=fields)
    store_resp.raise_for_status()

    cart_count = None
    count_el = BeautifulSoup(store_resp.text, "html.parser").select_one(".crt-count")
    if count_el:
        try:
            cart_count = int(count_el.get_text(strip=True))
        except ValueError:
            pass

    return AddToCartResult(
        item_name=cart_item.name,
        success=True,
        line_price_ngn=final_price,
        cart_count_after=cart_count,
        notes="; ".join(warnings) if warnings else None,
    )


def add_items_to_cart(inventory: MenuInventoryResult, items: list[CartItem]) -> list[AddToCartResult]:
    with httpx.Client(timeout=20, follow_redirects=True) as http:
        csrf_token = _get_csrf_token(http)
        return [_add_single_item_to_cart(http, csrf_token, inventory, item) for item in items]


# ---------------------------------------------------------------------------
# Phase 5, submit checkout via direct HTTP POST (replicating /place-order)
# ---------------------------------------------------------------------------
#
# #chk-form is NOT a real form submit, its button ($('#form-sub')) is
# handled entirely in JS: a confirm dialog, then a plain AJAX POST to
# /place-order with a hand-built data object (not the raw form fields).
# We replicate that exact payload. The JSON response tells us whether
# payment is needed and, if so, gives us the Paystack public key, amount,
# email, and reference needed to open the SAME payment popup separately —
# see get_paystack_transfer_details() for that step.
#
# Cart-building and checkout MUST share the same session (same cookies),
# so this opens ONE httpx.Client for the whole add-to-cart-then-checkout
# flow rather than reusing add_items_to_cart()'s own short-lived client.

DEFAULT_LATITUDE = "6.4444544"
DEFAULT_LONGITUDE = "3.4281205"


def _get_delivery_areas(http: httpx.Client) -> dict:
    """Scrape the checkout page's area dropdown fresh, rather than
    hardcoding it, since delivery zones/charges could change."""
    resp = http.get(f"{SITE_URL}/checkout")
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    areas = {}
    for option in soup.select("#area option[value]"):
        value = option.get("value", "").strip()
        if not value:
            continue
        name = option.get_text(strip=True)
        areas[name.lower()] = value
    return areas


def place_order(inventory: MenuInventoryResult, items: list[CartItem], checkout: CheckoutInfo) -> tuple[list[AddToCartResult], PlaceOrderResult]:
    with httpx.Client(timeout=20, follow_redirects=True) as http:
        csrf_token = _get_csrf_token(http)

        cart_results = [_add_single_item_to_cart(http, csrf_token, inventory, item) for item in items]
        if not any(r.success for r in cart_results):
            return cart_results, PlaceOrderResult(success=False, notes="No items were successfully added to cart")

        is_delivery = checkout.delivery_or_pickup.strip().lower() == "delivery"

        area_id = ""
        if is_delivery:
            areas = _get_delivery_areas(http)
            if checkout.area and checkout.area.strip().lower() in areas:
                area_id = areas[checkout.area.strip().lower()]
            else:
                order_result = PlaceOrderResult(
                    success=False,
                    notes=f"Delivery area '{checkout.area}' not recognized. Valid areas: {list(areas.keys())}",
                )
                return cart_results, order_result

        payload = {
            "_token": csrf_token,
            "type": "delivery" if is_delivery else "takeaway",
            "name": checkout.full_name,
            "email": checkout.email or "",
            "phone": checkout.phone,
            "area_id": area_id,
            "address": checkout.address if is_delivery else "",
            "latitude": DEFAULT_LATITUDE,
            "longitude": DEFAULT_LONGITUDE,
            "comments": checkout.notes or "",
            "d_notes": "",
            "entrance_code": "",
            "password": "",
            "password_confirmation": "",
            "save_info": "0",
        }

        resp = http.post(f"{SITE_URL}/place-order", data=payload, headers={"X-Requested-With": "XMLHttpRequest"})
        resp.raise_for_status()
        data = resp.json()

        if not data.get("status"):
            errors = data.get("errors") or data.get("error") or "Unknown error"
            return cart_results, PlaceOrderResult(success=False, notes=str(errors))

        order = data.get("order", {})
        order_result = PlaceOrderResult(
            success=True,
            order_identifier=order.get("identifier"),
            total_ngn=float(data.get("total", 0) or 0),
            payment_method=data.get("payment_method"),
            customer_email=order.get("email"),
            paystack_public_key="pk_live_23cec02dbf70060412938c8347ee693e8e7f7b39" if data.get("payment_method") == "paystack" else None,
        )
        return cart_results, order_result


# ---------------------------------------------------------------------------
# Phase 4 & 5, add to cart, fill checkout, read bank transfer details
# (stops before actually paying, no Monei call from here)
# ---------------------------------------------------------------------------

async def build_order_and_read_transfer_details(
    items: list[CartItem],
    checkout: CheckoutInfo,
) -> OrderResult:
    items_lines = []
    for i in items:
        line = f"- {i.quantity}x {i.name}"
        if i.option_preferences:
            prefs = "; ".join(f'"{group}" -> {", ".join(choices)}' for group, choices in i.option_preferences.items())
            line += f" (preferences: {prefs})"
        items_lines.append(line)
    items_str = "\n".join(items_lines)

    task = f"""
Go to {SITE_URL}. Do not log in, proceed as a guest if the site allows it.

Step 1, Add to cart:
Add these items to the cart, ONE AT A TIME, fully complete and verify each
one before starting the next:
{items_str}

{CAROUSEL_WARNING}

For each item:
1. Click that item's "Add to cart" link in the Main Menu category section.
   This opens a modal (id="addToCartModal") built fresh via an AJAX call —
   wait for it to actually appear and confirm its title matches the item
   you clicked before proceeding (if it shows a different item name, you
   clicked the wrong element, close it and retry on the correct item).

2. The modal may contain option groups. Handle each by its type:
   - If the item above has a listed preference for a group (matching by
     group name/title), select that exact choice regardless of whether the
     group is required or optional.
   - Otherwise, a group is REQUIRED if its container has a "val-required"
     class, its title has an asterisk (*), or (for "upgrade-option"
     blocks) its data-min attribute is greater than 0. Everything else is
     OPTIONAL.
   - REQUIRED "single-selection" group (radio-style, pick one) with no
     preference given: click the CHEAPEST option's label inside it (check
     each option's price, not just the first one).
   - REQUIRED "repeat-selections" or "upgrade-option" group (pick N items)
     with no preference given: click the "+" (upgrade-opt-up) button once
     on each of the N CHEAPEST options, where N is the data-min value.
   - OPTIONAL groups with no preference given: leave them completely
     untouched.
   - The "UPGRADE" toggle block: leave its toggle switch OFF/unchecked
     unless a preference explicitly asks for it.

3. Click the "Add to cart" SUBMIT BUTTON, this is the button with
   type="submit" inside the modal's own form (id="cart-form"), found at
   the bottom near the price and quantity stepper. This is a DIFFERENT
   element from the "Add to cart" link you clicked in step 1 to open the
   modal, even though both show the same text.

4. After the modal closes, verify the cart badge/count (".crt-count" in
   the navbar) actually increased before moving on to the next item.

Once all items are added, extract the cart contents one final time to
confirm both items and their correct quantities are present before moving
to checkout.

Step 2, Checkout:
Open the cart and proceed to checkout. Fill in the checkout form with:
  Full name: {checkout.full_name}
  Phone: {checkout.phone}
  Email: {checkout.email or "skip if not required"}
  Address: {checkout.address}
  Delivery or pickup: {checkout.delivery_or_pickup}
  Notes: {checkout.notes or "none"}
Submit the checkout form so the payment modal appears.

Step 3, Read bank transfer details (DO NOT PAY):
In the payment modal, select the "bank transfer" tab/option if there is a
choice of payment methods. Read off exactly:
  - the bank name
  - the account number
  - the account name (if shown)
  - the exact amount to pay
  - any order reference/narration shown

Stop as soon as you have read these details. Do NOT enter any card details,
do NOT click any "confirm payment" or "I have paid" button. Your job ends
at reading the transfer details.

When you have gathered all the information needed, finish by calling the
`done` action directly with the structured result. Do NOT use write_file
or any other tool to produce your output, `done` is the only way to
complete this task.
""".strip()

    agent = Agent(
        task=task,
        llm=_llm(),
        output_model_schema=OrderResult,
        headless=HEADLESS,
    )
    history = await agent.run(max_steps=40)

    result = history.structured_output
    if result is not None:
        return result
    raw = history.final_result()
    print("WARNING: structured_output was empty, falling back to raw text:")
    print(raw)
    return OrderResult.model_validate_json(raw)


# ---------------------------------------------------------------------------
# Phase 6, read bank transfer details directly from Paystack's own popup
# ---------------------------------------------------------------------------
#
# The transfer details (bank name, account number, amount) are NOT on
# Jay's Diner's site at all, they come from Paystack's own checkout app
# (a separate Vue SPA at checkout.paystack.com), which generates a
# one-time virtual account dynamically. We don't need to navigate Jay's
# Diner's checkout page in a browser to get this, Paystack's popup only
# needs four values (public key, email, amount, reference), all of which
# came back from place_order(). So we build a minimal standalone page that
# mounts the SAME Paystack widget directly, open just that in Playwright
# (plain Playwright, no AI agent needed, this is a narrow, fixed task),
# click "Transfer", and read three fixed elements by their data-testid.
#
# Served over a real http://localhost origin (not file://) since Paystack's
# iframe likely relies on window.postMessage origin checks that behave
# differently under file:// URLs.

def _build_paystack_page(public_key: str, email: str, amount_kobo: int, reference: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><script src="https://js.paystack.co/v1/inline.js"></script></head>
<body>
<script>
  var handler = PaystackPop.setup({{
    key: "{public_key}",
    email: "{email}",
    amount: {amount_kobo},
    ref: "{reference}",
    onClose: function() {{}},
    callback: function(response) {{}}
  }});
  handler.openIframe();
</script>
</body></html>"""


def get_paystack_transfer_details(order: PlaceOrderResult) -> TransferDetails:
    if not order.success or order.payment_method != "paystack":
        raise ValueError("Order was not successfully placed with Paystack as the payment method")
    if not order.paystack_public_key or not order.customer_email or not order.total_ngn or not order.order_identifier:
        raise ValueError("PlaceOrderResult is missing required fields to open the Paystack popup")

    amount_kobo = int(round(order.total_ngn * 100))
    html = _build_paystack_page(order.paystack_public_key, order.customer_email, amount_kobo, order.order_identifier)

    tmp_dir = tempfile.mkdtemp()
    page_path = os.path.join(tmp_dir, "pay.html")
    with open(page_path, "w") as f:
        f.write(html)

    original_cwd = os.getcwd()
    os.chdir(tmp_dir)
    httpd = socketserver.TCPServer(("localhost", 0), http.server.SimpleHTTPRequestHandler)
    port = httpd.server_address[1]
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS)
            page = browser.new_page()
            page.goto(f"http://localhost:{port}/pay.html")

            paystack_frame = None
            for _ in range(30):
                for frame in page.frames:
                    if "checkout.paystack.com" in frame.url:
                        paystack_frame = frame
                        break
                if paystack_frame:
                    break
                page.wait_for_timeout(500)

            if not paystack_frame:
                raise RuntimeError("Paystack checkout iframe never appeared within 15s")

            paystack_frame.click('[data-testid="bank_transfer-nav"]')
            paystack_frame.wait_for_selector('[data-testid="managed-account-bank"]', timeout=20000)

            bank_name = paystack_frame.locator('[data-testid="managed-account-bank"]').inner_text().strip()
            account_number_raw = paystack_frame.locator('[data-testid="managed-account-number"]').inner_text()
            amount_raw = paystack_frame.locator('[data-testid="amount-due"]').inner_text()

            browser.close()
    finally:
        httpd.shutdown()
        os.chdir(original_cwd)

    account_number = re.sub(r"[^\d]", "", account_number_raw)
    amount_clean = re.sub(r"[^\d.]", "", amount_raw)
    amount_value = float(amount_clean) if amount_clean else order.total_ngn

    return TransferDetails(
        bank_name=bank_name,
        account_number=account_number,
        account_name=None,
        amount_ngn=amount_value,
        reference=order.order_identifier,
    )