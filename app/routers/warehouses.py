from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, require_admin
from app.models import User, Warehouse
from app.schemas import WarehouseCreate, WarehouseOut, WarehouseUpdate

router = APIRouter(prefix="/warehouses", tags=["warehouses"])


@router.get("", response_model=list[WarehouseOut])
async def list_warehouses(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(Warehouse))
    return result.scalars().all()


@router.post("", response_model=WarehouseOut, status_code=status.HTTP_201_CREATED)
async def create_warehouse(
    data: WarehouseCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    warehouse = Warehouse(**data.model_dump())
    db.add(warehouse)
    await db.commit()
    await db.refresh(warehouse)
    return warehouse


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


@router.patch("/{warehouse_id}", response_model=WarehouseOut)
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

    await db.commit()
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
