from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class VendorCreate(BaseModel):
    name: str = Field(min_length=1)
    contact_info: str | None = None
    payment_terms: str | None = None


class VendorUpdate(BaseModel):
    name: str | None = None
    contact_info: str | None = None
    payment_terms: str | None = None
    active: bool | None = None


class VendorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    contact_info: str | None
    payment_terms: str | None
    active: bool
    created_at: datetime
    updated_at: datetime


class VendorDetail(VendorOut):
    total_parts: int
    total_quantity_available: float
    last_import_at: datetime | None = None
    last_import_status: str | None = None
