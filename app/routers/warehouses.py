import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.dialects.postgresql import NUMERIC
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, require_admin
from app.models import Product, StockMovement, User, Warehouse
from app.redis_client import VALUATION_CACHE_KEY, VALUATION_CACHE_TTL, invalidate_valuation_cache, redis_client
from app.schemas import WarehouseCreate, WarehouseOut, WarehouseUpdate, WarehouseValuation

router = APIRouter(prefix="/warehouses", tags=["warehouses"])


@router.get("", response_model=list[WarehouseOut])
async def list_warehouses(
    include_inactive: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    stmt = select(Warehouse)
    if not include_inactive:
        stmt = stmt.where(Warehouse.is_active == True)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post(
    "",
    response_model=WarehouseOut,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "location_code already exists"}},
)
async def create_warehouse(
    data: WarehouseCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    warehouse = Warehouse(**data.model_dump())
    db.add(warehouse)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Warehouse with location_code '{data.location_code}' already exists",
        )
    await invalidate_valuation_cache()
    await db.refresh(warehouse)
    return warehouse


# /valuation должен быть ДО /{warehouse_id}, иначе FastAPI поймает "valuation" как id
@router.get("/valuation", response_model=list[WarehouseValuation])
async def get_financial_valuation(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    cached = await redis_client.get(VALUATION_CACHE_KEY)
    if cached:
        return [WarehouseValuation(**item) for item in json.loads(cached)]

    # Один запрос: для каждого склада считаем стоимость остатков.
    # CASE: движение "в склад" → +qty*price, "со склада" → -qty*price.
    # literal(0).cast(NUMERIC) гарантирует, что COALESCE вернёт "0.00", а не "0".
    stmt = (
        select(
            Warehouse.id.label("warehouse_id"),
            Warehouse.name,
            func.coalesce(
                func.sum(
                    case(
                        (StockMovement.to_warehouse_id == Warehouse.id, StockMovement.quantity * Product.base_price),
                        (StockMovement.from_warehouse_id == Warehouse.id, -StockMovement.quantity * Product.base_price),
                        else_=0,
                    )
                ),
                literal(0).cast(NUMERIC(12, 2)),
            ).label("total_value"),
        )
        .outerjoin(
            StockMovement,
            or_(
                StockMovement.to_warehouse_id == Warehouse.id,
                StockMovement.from_warehouse_id == Warehouse.id,
            ),
        )
        .outerjoin(Product, Product.id == StockMovement.product_id)
        .group_by(Warehouse.id, Warehouse.name)
        .order_by(Warehouse.id)
    )

    result = await db.execute(stmt)
    valuations = [
        WarehouseValuation(warehouse_id=row.warehouse_id, name=row.name, total_value=row.total_value)
        for row in result.all()
    ]

    # Decimal несериализуем в JSON — конвертируем в строку; Pydantic восстановит обратно
    await redis_client.set(
        VALUATION_CACHE_KEY,
        json.dumps([
            {"warehouse_id": v.warehouse_id, "name": v.name, "total_value": str(v.total_value)}
            for v in valuations
        ]),
        ex=VALUATION_CACHE_TTL,
    )

    return valuations


@router.get("/{warehouse_id}", response_model=WarehouseOut)
async def get_warehouse(
    warehouse_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(Warehouse).where(Warehouse.id == warehouse_id))
    warehouse = result.scalar_one_or_none()
    if not warehouse:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Warehouse not found")
    return warehouse


@router.patch(
    "/{warehouse_id}",
    response_model=WarehouseOut,
    responses={409: {"description": "location_code already exists"}},
)
async def update_warehouse(
    warehouse_id: int,
    data: WarehouseUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(select(Warehouse).where(Warehouse.id == warehouse_id))
    warehouse = result.scalar_one_or_none()
    if not warehouse:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Warehouse not found")

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(warehouse, key, value)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Warehouse with this location_code already exists",
        )
    await invalidate_valuation_cache()
    await db.refresh(warehouse)
    return warehouse


@router.delete("/{warehouse_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_warehouse(
    warehouse_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(select(Warehouse).where(Warehouse.id == warehouse_id))
    warehouse = result.scalar_one_or_none()
    if not warehouse:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Warehouse not found")

    warehouse.is_active = False
    await db.commit()
