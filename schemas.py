from typing import Optional
from pydantic import BaseModel, Field


# ---------- Shared: option groups (used by check_order_options) ----------

class OptionChoice(BaseModel):
    label: str
    extra_price_ngn: Optional[float] = None


class OptionGroup(BaseModel):
    name: str
    required: bool = Field(description="True if this group has the 'val-required' class / an asterisk in its title")
    min_required: Optional[int] = Field(
        default=None, description="e.g. 2 for 'Choose Two Side', number of choices that must be picked"
    )
    choices: list[OptionChoice] = Field(default_factory=list)


# ---------- Phase 1: menu inventory (cheap, cacheable, no modals opened) ----------

class MenuItem(BaseModel):
    name: str = Field(description="Exact item name as written on the site")
    price_ngn: float
    category: Optional[str] = Field(default=None, description="e.g. 'Tenders', 'Pasta', 'Combo Meals'")
    product_id: Optional[str] = Field(default=None, description="The site's internal product ID, used to fetch its add-to-cart modal directly")
    image_url: Optional[str] = Field(default=None, description="Product thumbnail image URL, for rendering menu UI")
    notes: Optional[str] = None


class MenuInventoryResult(BaseModel):
    store_accepting_orders: bool = Field(
        description="False if the site shows any banner suspending delivery/orders"
    )
    store_status_note: Optional[str] = None
    items: list[MenuItem]


# ---------- Phase 2: matching the user's request against the inventory (non-browser) ----------

class MatchedItem(BaseModel):
    requested_name: str = Field(description="The item name as the user asked for it")
    matched: bool
    matched_name: Optional[str] = Field(default=None, description="Exact menu item name, if a match was found")
    price_ngn: Optional[float] = None
    notes: Optional[str] = Field(default=None, description="e.g. 'closest match to request'")


class MatchResult(BaseModel):
    matches: list[MatchedItem]


# ---------- Phase 3: order options discovery (opens modals, reads only, doesn't submit) ----------

class ItemOptions(BaseModel):
    matched_name: str
    option_groups: list[OptionGroup] = Field(
        default_factory=list,
        description="Empty list if the item has no customization options at all",
    )


class CheckOrderOptionsResult(BaseModel):
    items: list[ItemOptions]


# ---------- Phase 4 input: checkout info (collected from the user in chat) ----------

class CheckoutInfo(BaseModel):
    full_name: str
    phone: str
    email: Optional[str] = None
    address: str
    delivery_or_pickup: str = "delivery"
    area: Optional[str] = Field(
        default=None, description="Delivery area name (e.g. 'Ikoyi', 'Lekki Phase 1'), required for delivery orders"
    )
    notes: Optional[str] = None


class CartItem(BaseModel):
    name: str
    quantity: int = 1
    option_preferences: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Map of option group name -> desired choice label(s), case-insensitive "
        "(e.g. {'Choose Sauce': ['Pink Sauce']}). Omit a group to get an automatic default: "
        "cheapest choice for a required group, left untouched for an optional one.",
    )


class AddToCartResult(BaseModel):
    item_name: str
    success: bool
    line_price_ngn: Optional[float] = None
    cart_count_after: Optional[int] = None
    notes: Optional[str] = None


class PlaceOrderResult(BaseModel):
    success: bool
    order_identifier: Optional[str] = Field(default=None, description="Paystack/Monnify transaction reference")
    total_ngn: Optional[float] = None
    payment_method: Optional[str] = Field(default=None, description="'paystack' or 'monnify'")
    customer_email: Optional[str] = None
    paystack_public_key: Optional[str] = None
    notes: Optional[str] = None


# ---------- Phase 5: bank transfer details read off the payment modal ----------

class TransferDetails(BaseModel):
    bank_name: str
    account_number: str
    account_name: Optional[str] = None
    amount_ngn: float
    reference: Optional[str] = Field(
        default=None, description="Order reference/narration if the modal shows one"
    )


class OrderResult(BaseModel):
    cart_confirmed: bool = Field(description="True if all requested items were added to cart")
    checkout_submitted: bool
    transfer_details: Optional[TransferDetails] = None
    raw_notes: Optional[str] = Field(
        default=None, description="Anything unexpected the agent ran into"
    )