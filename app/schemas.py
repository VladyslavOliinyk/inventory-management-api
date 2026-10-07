from decimal import Decimal

from pydantic import BaseModel

from app.models import UserRole


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
