from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models import MovementType, Product, StockMovement, User
from app.schemas import BatchMovementCreate, MovementCreate, MovementOut

router = APIRouter(prefix="/movements", tags=["movements"])


async def get_balance(db: AsyncSession, product_id: int, warehouse_id: int) -> int:
    """Остаток товара на складе = приход - расход по истории движений."""
    stmt = select(
        func.coalesce(
            func.sum(
                case((StockMovement.to_warehouse_id == warehouse_id, StockMovement.quantity), else_=0)
            )
            - func.sum(
                case((StockMovement.from_warehouse_id == warehouse_id, StockMovement.quantity), else_=0)
            ),
            0,
        )
    ).where(
        StockMovement.product_id == product_id,
        or_(
            StockMovement.to_warehouse_id == warehouse_id,
            StockMovement.from_warehouse_id == warehouse_id,
        ),
    )
    result = await db.execute(stmt)
    return int(result.scalar())


@router.post("", response_model=MovementOut, status_code=status.HTTP_201_CREATED)
async def create_movement(
    data: MovementCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if data.type == MovementType.IN:
        result = await db.execute(
            select(Product).where(Product.id == data.product_id, Product.is_deleted == False)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    if data.type in (MovementType.OUT, MovementType.TRANSFER):
        # SELECT FOR UPDATE блокирует строку товара до конца транзакции,
        # не давая другим запросам одновременно списать тот же остаток
        result = await db.execute(
            select(Product)
            .where(Product.id == data.product_id, Product.is_deleted == False)
            .with_for_update()
        )
        product = result.scalar_one_or_none()
        if not product:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

        balance = await get_balance(db, data.product_id, data.from_warehouse_id)
        if balance < data.quantity:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Insufficient stock: available {balance}, needed {data.quantity}",
            )

    movement = StockMovement(
        type=data.type,
        product_id=data.product_id,
        quantity=data.quantity,
        from_warehouse_id=data.from_warehouse_id,
        to_warehouse_id=data.to_warehouse_id,
        performed_by=user.id,
    )
    db.add(movement)
    await db.commit()
    await db.refresh(movement)
    return movement


@router.post("/batch", response_model=list[MovementOut], status_code=status.HTTP_201_CREATED)
async def create_batch_movement(
    data: BatchMovementCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if data.type in (MovementType.OUT, MovementType.TRANSFER):
        # Сортируем по product_id — все транзакции будут блокировать в одном порядке,
        # что исключает deadlock при параллельных batch-запросах
        sorted_items = sorted(data.items, key=lambda x: x.product_id)
        product_ids = [item.product_id for item in sorted_items]

        result = await db.execute(
            select(Product)
            .where(Product.id.in_(product_ids), Product.is_deleted == False)
            .order_by(Product.id)
            .with_for_update()
        )
        products = {p.id: p for p in result.scalars().all()}

        # Проверяем остатки по всем позициям до создания движений
        insufficient = []
        for item in sorted_items:
            product = products.get(item.product_id)
            if not product:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Product {item.product_id} not found",
                )
            balance = await get_balance(db, item.product_id, data.from_warehouse_id)
            if balance < item.quantity:
                insufficient.append({
                    "product_id": item.product_id,
                    "sku": product.sku,
                    "needed": item.quantity,
                    "available": balance,
                })

        # Если не хватает хотя бы одной позиции — откатываем всё
        if insufficient:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"message": "Insufficient stock", "items": insufficient},
            )

    # Все проверки прошли — создаём движения в одной транзакции
    movements = [
        StockMovement(
            type=data.type,
            product_id=item.product_id,
            quantity=item.quantity,
            from_warehouse_id=data.from_warehouse_id,
            to_warehouse_id=data.to_warehouse_id,
            performed_by=user.id,
        )
        for item in data.items
    ]
    db.add_all(movements)
    await db.flush()
    await db.commit()
    for m in movements:
        await db.refresh(m)
    return movements
