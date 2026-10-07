from collections import defaultdict

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models import MovementType, Product, StockMovement, User, Warehouse
from app.notifications import notify_low_stock
from app.redis_client import invalidate_valuation_cache
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


async def check_active_warehouse(db: AsyncSession, warehouse_id: int) -> None:
    result = await db.execute(select(Warehouse).where(Warehouse.id == warehouse_id))
    wh = result.scalar_one_or_none()
    if not wh:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Warehouse {warehouse_id} not found",
        )
    if not wh.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Warehouse {warehouse_id} is not active",
        )


@router.post(
    "",
    response_model=MovementOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"description": "Warehouse is not active"},
        409: {"description": "Insufficient stock"},
    },
)
async def create_movement(
    data: MovementCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if data.to_warehouse_id is not None:
        await check_active_warehouse(db, data.to_warehouse_id)
    if data.from_warehouse_id is not None:
        await check_active_warehouse(db, data.from_warehouse_id)

    if data.type == MovementType.IN:
        result = await db.execute(
            select(Product).where(Product.id == data.product_id, Product.is_deleted == False)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    product = None
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
                status_code=status.HTTP_409_CONFLICT,
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
    await invalidate_valuation_cache()

    # Проверяем остаток после commit — если упал ниже порога, шлём уведомление фоном
    if product is not None and data.from_warehouse_id is not None:
        new_balance = await get_balance(db, product.id, data.from_warehouse_id)
        if new_balance < product.min_stock_level:
            background_tasks.add_task(
                notify_low_stock,
                product.sku,
                product.name,
                data.from_warehouse_id,
                new_balance,
                product.min_stock_level,
            )

    await db.refresh(movement)
    return movement


@router.post(
    "/batch",
    response_model=list[MovementOut],
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"description": "Warehouse is not active"},
        404: {"description": "Products not found"},
        409: {"description": "Insufficient stock"},
    },
)
async def create_batch_movement(
    data: BatchMovementCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if data.from_warehouse_id is not None:
        await check_active_warehouse(db, data.from_warehouse_id)
    if data.to_warehouse_id is not None:
        await check_active_warehouse(db, data.to_warehouse_id)

    # Суммируем количества по SKU (один SKU может встречаться несколько раз)
    quantity_by_sku: dict[str, int] = defaultdict(int)
    for item in data.items:
        quantity_by_sku[item.sku] += item.quantity

    skus = list(quantity_by_sku.keys())

    if data.type in (MovementType.OUT, MovementType.TRANSFER):
        # Сортируем по id — все транзакции будут блокировать в одном порядке,
        # что исключает deadlock при параллельных batch-запросах
        result = await db.execute(
            select(Product)
            .where(Product.sku.in_(skus), Product.is_deleted == False)
            .order_by(Product.id)
            .with_for_update()
        )
    else:
        result = await db.execute(
            select(Product).where(Product.sku.in_(skus), Product.is_deleted == False)
        )

    products_by_sku = {p.sku: p for p in result.scalars().all()}

    # Возвращаем все неизвестные SKU одной ошибкой
    unknown = [sku for sku in skus if sku not in products_by_sku]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "Products not found", "skus": unknown},
        )

    if data.type in (MovementType.OUT, MovementType.TRANSFER):
        product_ids = [p.id for p in products_by_sku.values()]

        # Один запрос: остатки всех товаров на складе-источнике
        balance_result = await db.execute(
            select(
                StockMovement.product_id,
                func.coalesce(
                    func.sum(
                        case((StockMovement.to_warehouse_id == data.from_warehouse_id, StockMovement.quantity), else_=0)
                    )
                    - func.sum(
                        case((StockMovement.from_warehouse_id == data.from_warehouse_id, StockMovement.quantity), else_=0)
                    ),
                    0,
                ).label("balance"),
            )
            .where(
                StockMovement.product_id.in_(product_ids),
                or_(
                    StockMovement.to_warehouse_id == data.from_warehouse_id,
                    StockMovement.from_warehouse_id == data.from_warehouse_id,
                ),
            )
            .group_by(StockMovement.product_id)
        )
        balances = {row.product_id: row.balance for row in balance_result.all()}

        insufficient = []
        for sku, total_qty in quantity_by_sku.items():
            product = products_by_sku[sku]
            available = balances.get(product.id, 0)
            if available < total_qty:
                insufficient.append({"sku": sku, "requested": total_qty, "available": available})

        # Если не хватает хотя бы одной позиции — откатываем всё
        if insufficient:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"message": "Insufficient stock", "items": insufficient},
            )

    # Все проверки прошли — создаём движения в одной транзакции
    movements = [
        StockMovement(
            type=data.type,
            product_id=products_by_sku[item.sku].id,
            quantity=item.quantity,
            from_warehouse_id=data.from_warehouse_id,
            to_warehouse_id=data.to_warehouse_id,
            performed_by=user.id,
        )
        for item in data.items
    ]
    db.add_all(movements)
    await db.commit()
    await invalidate_valuation_cache()

    # Проверяем остатки после commit для всех списанных товаров
    if data.type in (MovementType.OUT, MovementType.TRANSFER) and data.from_warehouse_id is not None:
        product_ids = [p.id for p in products_by_sku.values()]
        new_balance_result = await db.execute(
            select(
                StockMovement.product_id,
                func.coalesce(
                    func.sum(
                        case((StockMovement.to_warehouse_id == data.from_warehouse_id, StockMovement.quantity), else_=0)
                    )
                    - func.sum(
                        case((StockMovement.from_warehouse_id == data.from_warehouse_id, StockMovement.quantity), else_=0)
                    ),
                    0,
                ).label("balance"),
            )
            .where(
                StockMovement.product_id.in_(product_ids),
                or_(
                    StockMovement.to_warehouse_id == data.from_warehouse_id,
                    StockMovement.from_warehouse_id == data.from_warehouse_id,
                ),
            )
            .group_by(StockMovement.product_id)
        )
        new_balances = {row.product_id: row.balance for row in new_balance_result.all()}

        for product in products_by_sku.values():
            new_bal = new_balances.get(product.id, 0)
            if new_bal < product.min_stock_level:
                background_tasks.add_task(
                    notify_low_stock,
                    product.sku,
                    product.name,
                    data.from_warehouse_id,
                    new_bal,
                    product.min_stock_level,
                )

    for m in movements:
        await db.refresh(m)
    return movements
