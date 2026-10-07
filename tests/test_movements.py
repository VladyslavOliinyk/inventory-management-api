import asyncio

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _create_warehouse(client: AsyncClient, headers: dict, code: str) -> int:
    r = await client.post("/warehouses", json={
        "name": f"Warehouse {code}",
        "location_code": code,
        "address": "Test St 1",
    }, headers=headers)
    assert r.status_code == 201
    return r.json()["id"]


async def _create_product(client: AsyncClient, headers: dict, sku: str) -> dict:
    r = await client.post("/products", json={
        "sku": sku,
        "name": f"Product {sku}",
        "base_price": "100.00",
        "min_stock_level": 0,
    }, headers=headers)
    assert r.status_code == 201
    return r.json()


async def _in(client: AsyncClient, headers: dict, product_id: int, warehouse_id: int, qty: int):
    return await client.post("/movements", json={
        "type": "IN",
        "product_id": product_id,
        "quantity": qty,
        "to_warehouse_id": warehouse_id,
    }, headers=headers)


async def _out(client: AsyncClient, headers: dict, product_id: int, warehouse_id: int, qty: int):
    return await client.post("/movements", json={
        "type": "OUT",
        "product_id": product_id,
        "quantity": qty,
        "from_warehouse_id": warehouse_id,
    }, headers=headers)


async def _transfer(
    client: AsyncClient, headers: dict,
    product_id: int, from_wh: int, to_wh: int, qty: int,
):
    return await client.post("/movements", json={
        "type": "TRANSFER",
        "product_id": product_id,
        "quantity": qty,
        "from_warehouse_id": from_wh,
        "to_warehouse_id": to_wh,
    }, headers=headers)


async def _get_total_balance(client: AsyncClient, headers: dict, sku: str) -> int:
    r = await client.get("/products", headers=headers)
    assert r.status_code == 200
    for product in r.json():
        if product["sku"] == sku:
            return product["total_balance"]
    raise AssertionError(f"Product with SKU '{sku}' not found in response")


async def test_race_condition_stock_never_goes_negative(client: AsyncClient, admin_headers: dict):
    """
    15 concurrent OUT requests against stock of 10.
    Exactly 10 must succeed (201) and 5 must get 409.
    Final balance must not go below zero.
    """
    wh_id = await _create_warehouse(client, admin_headers, "WH-RACE")
    product = await _create_product(client, admin_headers, "SKU-RACE")
    product_id = product["id"]

    r = await _in(client, admin_headers, product_id, wh_id, 10)
    assert r.status_code == 201

    responses = await asyncio.gather(
        *[_out(client, admin_headers, product_id, wh_id, 1) for _ in range(15)]
    )

    statuses = [r.status_code for r in responses]
    assert statuses.count(201) == 10, f"Expected 10 successes, got statuses: {statuses}"
    assert statuses.count(409) == 5, f"Expected 5 conflicts, got statuses: {statuses}"

    balance = await _get_total_balance(client, admin_headers, "SKU-RACE")
    assert balance == 0


async def test_batch_atomicity_rollback_on_insufficient_stock(client: AsyncClient, admin_headers: dict):
    """
    Batch OUT with one item short — entire operation must roll back.
    Balance of the other item must remain unchanged.
    """
    wh_id = await _create_warehouse(client, admin_headers, "WH-BATCH")
    product_a = await _create_product(client, admin_headers, "SKU-A")
    await _create_product(client, admin_headers, "SKU-B")

    r = await _in(client, admin_headers, product_a["id"], wh_id, 5)
    assert r.status_code == 201

    r = await client.post("/movements/batch", json={
        "type": "OUT",
        "from_warehouse_id": wh_id,
        "items": [
            {"sku": "SKU-A", "quantity": 3},
            {"sku": "SKU-B", "quantity": 2},
        ],
    }, headers=admin_headers)

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["message"] == "Insufficient stock"
    insufficient_skus = [item["sku"] for item in detail["items"]]
    assert "SKU-B" in insufficient_skus

    balance_a = await _get_total_balance(client, admin_headers, "SKU-A")
    assert balance_a == 5


async def test_idempotency_key_prevents_duplicate_movement(client: AsyncClient, admin_headers: dict):
    """
    Two sequential OUT requests with the same Idempotency-Key create exactly one
    movement. The second response is identical to the first.
    """
    wh_id = await _create_warehouse(client, admin_headers, "WH-IDEM")
    product = await _create_product(client, admin_headers, "SKU-IDEM")
    product_id = product["id"]

    r = await _in(client, admin_headers, product_id, wh_id, 10)
    assert r.status_code == 201

    idem_headers = {**admin_headers, "Idempotency-Key": "test-key-001"}

    r1 = await client.post("/movements", json={
        "type": "OUT",
        "product_id": product_id,
        "quantity": 3,
        "from_warehouse_id": wh_id,
    }, headers=idem_headers)
    assert r1.status_code == 201

    r2 = await client.post("/movements", json={
        "type": "OUT",
        "product_id": product_id,
        "quantity": 3,
        "from_warehouse_id": wh_id,
    }, headers=idem_headers)
    assert r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]

    balance = await _get_total_balance(client, admin_headers, "SKU-IDEM")
    assert balance == 7


async def test_balance_calculation_after_in_transfer_out(client: AsyncClient, admin_headers: dict):
    """
    IN(10) -> TRANSFER(4) -> OUT(3): total balance across all warehouses must be 7.
    TRANSFER moves stock between warehouses without changing the total.
    """
    wh1_id = await _create_warehouse(client, admin_headers, "WH-SRC")
    wh2_id = await _create_warehouse(client, admin_headers, "WH-DST")
    product = await _create_product(client, admin_headers, "SKU-CALC")
    product_id = product["id"]

    r = await _in(client, admin_headers, product_id, wh1_id, 10)
    assert r.status_code == 201

    r = await _transfer(client, admin_headers, product_id, wh1_id, wh2_id, 4)
    assert r.status_code == 201

    r = await _out(client, admin_headers, product_id, wh2_id, 3)
    assert r.status_code == 201

    balance = await _get_total_balance(client, admin_headers, "SKU-CALC")
    assert balance == 7
