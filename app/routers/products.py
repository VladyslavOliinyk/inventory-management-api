from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, require_admin
from app.models import MovementType, Product, StockMovement, User
from app.redis_client import invalidate_valuation_cache
from app.schemas import ProductCreate, ProductOut, ProductUpdate, ProductWithBalance

router = APIRouter(prefix="/products", tags=["products"])


@router.get("", response_model=list[ProductWithBalance])
async def list_products(
    min_balance: int | None = Query(default=None, ge=0),
    max_balance: int | None = Query(default=None, ge=0),
    min_price: Decimal | None = Query(default=None),
    max_price: Decimal | None = Query(default=None),
    sort_by: Literal["id", "price", "balance"] = Query(default="id"),
    order: Literal["asc", "desc"] = Query(default="asc"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    # Подзапрос: суммарный остаток по всем складам для каждого товара.
    # IN прибавляет к остатку, OUT вычитает, TRANSFER — внутренняя перекладка (итог 0).
    balance_subq = (
        select(
            StockMovement.product_id,
            func.coalesce(
                func.sum(
                    case((StockMovement.type == MovementType.IN, StockMovement.quantity), else_=0)
                )
                - func.sum(
                    case((StockMovement.type == MovementType.OUT, StockMovement.quantity), else_=0)
                ),
                0,
            ).label("total_balance"),
        )
        .group_by(StockMovement.product_id)
        .subquery()
    )

    balance_expr = func.coalesce(balance_subq.c.total_balance, 0)

    stmt = (
        select(Product, balance_expr.label("total_balance"))
        .outerjoin(balance_subq, Product.id == balance_subq.c.product_id)
        .where(Product.is_deleted == False)
    )

    if min_balance is not None:
        stmt = stmt.where(balance_expr >= min_balance)
    if max_balance is not None:
        stmt = stmt.where(balance_expr <= max_balance)
    if min_price is not None:
        stmt = stmt.where(Product.base_price >= min_price)
    if max_price is not None:
        stmt = stmt.where(Product.base_price <= max_price)

    if sort_by == "balance":
        primary = balance_expr
    elif sort_by == "price":
        primary = Product.base_price
    else:
        primary = Product.id

    # Вторичная сортировка по id — стабильный порядок при равных значениях
    sort_dir = primary.asc() if order == "asc" else primary.desc()
    stmt = stmt.order_by(sort_dir, Product.id.asc())
    stmt = stmt.limit(limit).offset(offset)

    result = await db.execute(stmt)
    return [
        ProductWithBalance(
            id=p.id,
            sku=p.sku,
            name=p.name,
            base_price=p.base_price,
            min_stock_level=p.min_stock_level,
            is_deleted=p.is_deleted,
            total_balance=balance,
        )
        for p, balance in result.all()
    ]


@router.post(
    "",
    response_model=ProductOut,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "SKU already exists"}},
)
async def create_product(
    data: ProductCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    product = Product(**data.model_dump())
    db.add(product)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Product with SKU '{data.sku}' already exists",
        )
    await db.refresh(product)
    return product


@router.get("/{product_id}", response_model=ProductOut)
async def get_product(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Product).where(Product.id == product_id, Product.is_deleted == False)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return product


@router.patch(
    "/{product_id}",
    response_model=ProductOut,
    responses={409: {"description": "SKU already exists"}},
)
async def update_product(
    product_id: int,
    data: ProductUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(
        select(Product).where(Product.id == product_id, Product.is_deleted == False)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(product, key, value)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Product with this SKU already exists",
        )
    # base_price могла измениться — финансовая оценка складов стала неактуальной
    await invalidate_valuation_cache()
    await db.refresh(product)
    return product


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(
        select(Product).where(Product.id == product_id, Product.is_deleted == False)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    # Soft-delete: не удаляем из БД, чтобы не потерять историю движений
    product.is_deleted = True
    await db.commit()
