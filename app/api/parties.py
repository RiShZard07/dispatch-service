"""Merchant and customer endpoints. Orders reference these ids, so they must exist first."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import EngineDep
from app.schemas import CustomerOut, MerchantCreate, MerchantOut
from app.services.parties import create_customer, create_merchant, get_customer, get_merchant

router = APIRouter(tags=["parties"])


@router.post("/merchants", response_model=MerchantOut, status_code=201)
def create_merchant_endpoint(payload: MerchantCreate, engine: EngineDep) -> MerchantOut:
    return create_merchant(engine, payload)


@router.get("/merchants/{merchant_id}", response_model=MerchantOut)
def show_merchant(merchant_id: str, engine: EngineDep) -> MerchantOut:
    return get_merchant(engine, merchant_id)


@router.post("/customers", response_model=CustomerOut, status_code=201)
def create_customer_endpoint(engine: EngineDep) -> CustomerOut:
    """Customers are synthetic ids only; no personal data is stored."""
    return create_customer(engine)


@router.get("/customers/{customer_id}", response_model=CustomerOut)
def show_customer(customer_id: str, engine: EngineDep) -> CustomerOut:
    return get_customer(engine, customer_id)
