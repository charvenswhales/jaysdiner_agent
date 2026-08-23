from typing import Optional

from pydantic import BaseModel


class CheckoutProfileCreate(BaseModel):
    label: str = "Home"
    full_name: str
    phone: str
    address: Optional[str] = None
    area: Optional[str] = None
    delivery_or_pickup: str = "delivery"
    is_default: bool = False


class CheckoutProfileUpdate(BaseModel):
    label: Optional[str] = None
    full_name: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    area: Optional[str] = None
    delivery_or_pickup: Optional[str] = None


class CheckoutProfileResponse(BaseModel):
    id: str
    label: str
    full_name: str
    phone: str
    address: Optional[str] = None
    area: Optional[str] = None
    delivery_or_pickup: str
    is_default: bool

    class Config:
        from_attributes = True