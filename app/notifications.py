import logging

logger = logging.getLogger(__name__)


def notify_low_stock(
    sku: str,
    name: str,
    warehouse_id: int,
    current_balance: int,
    min_stock_level: int,
) -> None:
    """Фоновая задача: логирует факт отправки email при низком остатке."""
    logger.warning(
        "[EMAIL] Low stock alert: product %s (%s) at warehouse %d — "
        "balance %d is below min_stock_level %d",
        sku,
        name,
        warehouse_id,
        current_balance,
        min_stock_level,
    )
