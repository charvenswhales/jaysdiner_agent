"""
Server-side calls to Monei's Connect token endpoint. Never called from the
frontend, client_secret must never leave the backend.
"""

import re
from datetime import datetime, timedelta
from typing import Optional, TypedDict

import httpx

from config import MONEI_CLIENT_ID, MONEI_CLIENT_SECRET, MONEI_REDIRECT_URI, MONEI_API_BASE


class TokenResult(TypedDict):
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
    scopes: list[str]


class MoneiTokenError(Exception):
    pass


class MoneiAPIError(Exception):
    pass


def exchange_code_for_tokens(code: str) -> TokenResult:
    response = httpx.post(
        f"{MONEI_API_BASE}/api/v1/connect/token",
        json={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": MONEI_CLIENT_ID,
            "client_secret": MONEI_CLIENT_SECRET,
            "redirect_uri": MONEI_REDIRECT_URI,
        },
        timeout=20,
    )
    if not response.is_success:
        raise MoneiTokenError(f"Token exchange failed ({response.status_code}): {response.text}")
    return response.json()


def refresh_access_token(refresh_token: str) -> TokenResult:
    response = httpx.post(
        f"{MONEI_API_BASE}/api/v1/connect/token",
        json={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": MONEI_CLIENT_ID,
            "client_secret": MONEI_CLIENT_SECRET,
        },
        timeout=20,
    )
    if not response.is_success:
        raise MoneiTokenError(f"Token refresh failed ({response.status_code}): {response.text}")
    return response.json()


def expires_at_from_now(expires_in_seconds: int) -> datetime:
    return datetime.utcnow() + timedelta(seconds=expires_in_seconds)


def get_wallet(access_token: str) -> dict:
    """GET /api/v1/wallet/me, returns { statusCode, message, data: {
    nairaBalance, evmPortfolio, solPortfolio, subwallets }, errors }."""
    response = httpx.get(
        f"{MONEI_API_BASE}/api/v1/wallet/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if not response.is_success:
        raise MoneiAPIError(f"Wallet fetch failed ({response.status_code}): {response.text}")
    return response.json()


def get_supported_banks(access_token: str) -> list[dict]:
    """GET /api/v1/wallet/utils/banks, [{id, code, name}, ...]."""
    response = httpx.get(
        f"{MONEI_API_BASE}/api/v1/wallet/utils/banks",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if not response.is_success:
        raise MoneiAPIError(f"Bank list fetch failed ({response.status_code}): {response.text}")
    return response.json().get("data", [])


def _extract_error_message(response: httpx.Response, fallback: str) -> str:
    """Monei's errors come back as {"message": "...", "error": "...",
    "statusCode": ...} (or occasionally message as a list of validation
    strings). Pull out the actual human message instead of surfacing the
    raw JSON blob to the user."""
    try:
        body = response.json()
        message = body.get("message")
        if isinstance(message, list):
            message = "; ".join(str(m) for m in message)
        return message or fallback
    except Exception:
        return fallback


def send_bank_transfer_payout(
    access_token: str,
    amount: float,
    bank_code: str,
    account_number: str,
    transaction_pin: str,
    reference: str,
    narration: str,
) -> dict:
    """POST /api/v1/wallet/payout/bank-transfer, requires the user's own
    Monei transaction PIN (a second factor beyond the OAuth token itself).
    Never call this with a PIN that came from anywhere except a direct,
    dedicated frontend input, it must never pass through agent/LLM
    context. Returns { reference, status, amount }."""
    response = httpx.post(
        f"{MONEI_API_BASE}/api/v1/wallet/payout/bank-transfer",
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "amount": amount,
            "bank": bank_code,
            "accountNumber": account_number,
            "transactionPin": transaction_pin,
            "reference": reference,
            "narration": narration,
        },
        timeout=30,
    )
    if not response.is_success:
        message = _extract_error_message(response, f"Payout failed with status {response.status_code}")
        raise MoneiAPIError(message)
    return response.json().get("data", {})


def classify_payout_error(message: str) -> str:
    """Turns Monei's raw error message into a stable error_code the
    frontend can branch on, a wrong PIN and an empty wallet are
    completely different situations and shouldn't get the same UI
    treatment (e.g. a wrong-PIN shake animation on an insufficient-funds
    error would be actively misleading)."""
    lowered = message.lower()
    if "insufficient" in lowered or "balance" in lowered:
        return "insufficient_funds"
    if "pin" in lowered:
        return "invalid_pin"
    if "bank" in lowered or "account" in lowered:
        return "invalid_account"
    return "payment_failed"


_BANK_NAME_STOPWORDS = {
    "bank", "banks", "of", "the", "limited", "ltd", "plc",
    "nigeria", "mfb", "microfinance", "company", "co",
}


def resolve_bank_code(paystack_bank_name: str, banks: list[dict]) -> Optional[str]:
    """Paystack's virtual account comes back with a bank name like
    'Paystack-Titan'. Monei's bank list might have a DISTINCT entry for
    that exact co-branded name (since a virtual-account rail can carry
    its own bank code for NIP routing, separate from the underlying
    bank's regular code), so we try the full original name FIRST, and
    only fall back to stripping known prefixes (matching the underlying
    bank, e.g. 'Titan Trust Bank') if nothing matches directly. Getting
    this order backwards would silently pick the wrong bank code whenever
    a distinct co-branded entry actually exists."""
    original = paystack_bank_name.strip().lower()
    if not original:
        return None

    # 1. Exact/substring match on the FULL original name first.
    for bank in banks:
        name = bank["name"].lower()
        if original == name or original in name or name in original:
            return bank["code"]

    # 2. Fall back to stripping a known co-brand prefix and matching the
    #    underlying bank name instead.
    cleaned = re.sub(r"^paystack-?", "", original, flags=re.IGNORECASE).strip()
    if cleaned and cleaned != original:
        for bank in banks:
            name = bank["name"].lower()
            if cleaned in name or name in cleaned:
                return bank["code"]
    else:
        cleaned = original

    # 3. Token-overlap fallback, ignoring generic banking words.
    cleaned_tokens = set(cleaned.split()) - _BANK_NAME_STOPWORDS
    if not cleaned_tokens:
        return None

    best_bank = None
    best_score = 0
    for bank in banks:
        name_tokens = set(bank["name"].lower().split()) - _BANK_NAME_STOPWORDS
        score = len(cleaned_tokens & name_tokens)
        if score > best_score:
            best_score = score
            best_bank = bank

    return best_bank["code"] if best_bank else None