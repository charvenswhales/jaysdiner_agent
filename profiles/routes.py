from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from db.base import get_db
from db.models import User, CheckoutProfile
from auth.dependencies import get_current_user
from profiles.schemas import (
    CheckoutProfileCreate,
    CheckoutProfileUpdate,
    CheckoutProfileResponse,
)

router = APIRouter(prefix="/profiles", tags=["checkout-profiles"])


def _get_owned_profile(profile_id: str, user: User, db: Session) -> CheckoutProfile:
    """Fetch a profile, scoped to the current user. 404 (not 403) if it
    doesn't exist OR belongs to someone else, never confirm another
    user's profile ID exists."""
    profile = (
        db.query(CheckoutProfile)
        .filter(CheckoutProfile.id == profile_id, CheckoutProfile.user_id == user.id)
        .first()
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Checkout profile not found")
    return profile


def _clear_other_defaults(user: User, db: Session, except_id: str = None) -> None:
    query = db.query(CheckoutProfile).filter(
        CheckoutProfile.user_id == user.id, CheckoutProfile.is_default == True  # noqa: E712
    )
    if except_id:
        query = query.filter(CheckoutProfile.id != except_id)
    query.update({"is_default": False})


@router.post("", response_model=CheckoutProfileResponse, status_code=201)
def create_profile(
    payload: CheckoutProfileCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    existing_count = (
        db.query(CheckoutProfile).filter(CheckoutProfile.user_id == current_user.id).count()
    )
    # The user's first profile is always the default, regardless of what
    # was passed in, there should never be zero default profiles once one exists.
    make_default = payload.is_default or existing_count == 0

    profile = CheckoutProfile(
        user_id=current_user.id,
        label=payload.label,
        full_name=payload.full_name,
        phone=payload.phone,
        address=payload.address,
        area=payload.area,
        delivery_or_pickup=payload.delivery_or_pickup,
        is_default=make_default,
    )
    db.add(profile)
    db.flush()  # get profile.id before the default-clearing update

    if make_default:
        _clear_other_defaults(current_user, db, except_id=profile.id)

    db.commit()
    db.refresh(profile)
    return profile


@router.get("", response_model=list[CheckoutProfileResponse])
def list_profiles(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(CheckoutProfile)
        .filter(CheckoutProfile.user_id == current_user.id)
        .order_by(CheckoutProfile.is_default.desc(), CheckoutProfile.created_at.asc())
        .all()
    )


@router.get("/{profile_id}", response_model=CheckoutProfileResponse)
def get_profile(
    profile_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _get_owned_profile(profile_id, current_user, db)


@router.patch("/{profile_id}", response_model=CheckoutProfileResponse)
def update_profile(
    profile_id: str,
    payload: CheckoutProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _get_owned_profile(profile_id, current_user, db)
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(profile, field, value)
    db.commit()
    db.refresh(profile)
    return profile


@router.post("/{profile_id}/set-default", response_model=CheckoutProfileResponse)
def set_default_profile(
    profile_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _get_owned_profile(profile_id, current_user, db)
    _clear_other_defaults(current_user, db, except_id=profile.id)
    profile.is_default = True
    db.commit()
    db.refresh(profile)
    return profile


@router.delete("/{profile_id}", status_code=204)
def delete_profile(
    profile_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _get_owned_profile(profile_id, current_user, db)
    was_default = profile.is_default
    db.delete(profile)
    db.flush()

    if was_default:
        # Promote the oldest remaining profile to default, if any are left
        next_profile = (
            db.query(CheckoutProfile)
            .filter(CheckoutProfile.user_id == current_user.id)
            .order_by(CheckoutProfile.created_at.asc())
            .first()
        )
        if next_profile:
            next_profile.is_default = True

    db.commit()
    return None