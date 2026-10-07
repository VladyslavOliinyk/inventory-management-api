from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, field_validator, model_validator

from app.models import MovementType, UserRole


# --- Auth ---

class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    email: str
    role: UserRole

    model_config = {"from_attributes": True}


# --- Warehouses ---

class WarehouseCreate(BaseModel):
    name: str
    location_code: str
    address: str
    is_active: bool = True


class WarehouseUpdate(BaseModel):
    name: str | None = None
    location_code: str | None = None
    address: str | None = None
    is_active: bool | None = None


class WarehouseOut(BaseModel):
    id: int
    name: str
    location_code: str
    address: str
    is_active: bool

    model_config = {"from_attributes": True}


# --- Products ---

class ProductCreate(BaseModel):
    sku: str
    name: str
    base_price: Decimal
    min_stock_level: int = 0


class ProductUpdate(BaseModel):
    name: str | None = None
    base_price: Decimal | None = None
    min_stock_level: int | None = None


class ProductOut(BaseModel):
    id: int
    sku: str
    name: str
    base_price: Decimal
    min_stock_level: int
    is_deleted: bool

    model_config = {"from_attributes": True}


class ProductWithBalance(ProductOut):
    total_balance: int


# --- Movements ---

class MovementCreate(BaseModel):
    type: MovementType
    product_id: int
    quantity: int
    from_warehouse_id: int | None = None
    to_warehouse_id: int | None = None

    @field_validator("quantity")
    @classmethod
    def quantity_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("quantity must be greater than 0")
        return v

    @model_validator(mode="after")
    def validate_warehouses(self) -> "MovementCreate":
        if self.type == MovementType.IN and not self.to_warehouse_id:
            raise ValueError("IN requires to_warehouse_id")
        if self.type == MovementType.OUT and not self.from_warehouse_id:
            raise ValueError("OUT requires from_warehouse_id")
        if self.type == MovementType.TRANSFER and (
            not self.from_warehouse_id or not self.to_warehouse_id
        ):
            raise ValueError("TRANSFER requires both from_warehouse_id and to_warehouse_id")
        if (
            self.type == MovementType.TRANSFER
            and self.from_warehouse_id == self.to_warehouse_id
        ):
            raise ValueError("TRANSFER requires different from and to warehouses")
        return self


class WarehouseValuation(BaseModel):
    warehouse_id: int
    name: str
    total_value: Decimal


class MovementOut(BaseModel):
    id: int
    type: MovementType
    product_id: int
    quantity: int
    from_warehouse_id: int | None
    to_warehouse_id: int | None
    performed_by: int
    created_at: datetime

    model_config = {"from_attributes": True}


class BatchItem(BaseModel):
    sku: str
    quantity: int

    @field_validator("quantity")
    @classmethod
    def quantity_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("quantity must be greater than 0")
        return v


class BatchMovementCreate(BaseModel):
    type: MovementType
    from_warehouse_id: int | None = None
    to_warehouse_id: int | None = None
    items: list[BatchItem]

    @model_validator(mode="after")
    def validate_warehouses(self) -> "BatchMovementCreate":
        if self.type == MovementType.IN and not self.to_warehouse_id:
            raise ValueError("IN batch requires to_warehouse_id")
        if self.type == MovementType.OUT and not self.from_warehouse_id:
            raise ValueError("OUT batch requires from_warehouse_id")
        if self.type == MovementType.TRANSFER and (
            not self.from_warehouse_id or not self.to_warehouse_id
        ):
            raise ValueError("TRANSFER batch requires both warehouses")
        if (
            self.type == MovementType.TRANSFER
            and self.from_warehouse_id == self.to_warehouse_id
        ):
            raise ValueError("TRANSFER requires different from and to warehouses")
        return self
