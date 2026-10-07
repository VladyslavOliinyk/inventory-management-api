# Inventory Management API

Async REST API for warehouse stock management built with FastAPI.

## Stack

- **Python 3.12**, FastAPI, Uvicorn
- **PostgreSQL 16** + SQLAlchemy 2.0 async + asyncpg + Alembic
- **Redis 7** — financial valuation cache
- **JWT** authentication (python-jose + bcrypt)
- **pytest** + pytest-asyncio + httpx — integration tests
- **Docker Compose**

## Features

- Stock balance calculated dynamically from movement history (no balance field in DB)
- Race condition protection via `SELECT ... FOR UPDATE`
- Atomic batch operations — full rollback if any item is short
- Single-query warehouse financial valuation with Redis cache (TTL 300s, invalidated on every movement)
- Background email notification when stock drops below `min_stock_level`
- Role-based access: **Admin** and **Storekeeper**

## Quick start

```bash
cp .env.example .env
docker compose up --build -d
docker compose run --rm app alembic upgrade head
docker compose run --rm app python seed.py
```

API is available at http://localhost:8000
Interactive docs: http://localhost:8000/docs

## Seed users

| Role        | Email                      | Password   |
|-------------|----------------------------|------------|
| Admin       | admin@example.com          | admin123   |
| Storekeeper | storekeeper@example.com    | store123   |

## Environment variables

Copy `.env.example` to `.env` and adjust values:

| Variable           | Default          | Description                        |
|--------------------|------------------|------------------------------------|
| `POSTGRES_USER`    | inventory        | PostgreSQL user                    |
| `POSTGRES_PASSWORD`| inventory_pass   | PostgreSQL password                |
| `POSTGRES_DB`      | inventory_db     | PostgreSQL database name           |
| `POSTGRES_HOST`    | localhost        | PostgreSQL host                    |
| `POSTGRES_PORT`    | 5433             | PostgreSQL port (host mapping)     |
| `REDIS_URL`        | redis://localhost:6379 | Redis connection URL         |
| `SECRET_KEY`       | —                | JWT signing secret                 |
| `DB_ECHO`          | False            | Log SQL queries to stdout          |

## API endpoints

### Auth
| Method | Path         | Access | Description      |
|--------|--------------|--------|------------------|
| POST   | /auth/login  | Public | Get JWT token    |

### Warehouses
| Method | Path                     | Access      | Description                          |
|--------|--------------------------|-------------|--------------------------------------|
| GET    | /warehouses              | Any         | List warehouses (`?include_inactive`) |
| POST   | /warehouses              | Admin       | Create warehouse                     |
| GET    | /warehouses/{id}         | Any         | Get warehouse                        |
| PATCH  | /warehouses/{id}         | Admin       | Update warehouse                     |
| DELETE | /warehouses/{id}         | Admin       | Deactivate warehouse                 |
| GET    | /warehouses/valuation    | Admin       | Financial valuation per warehouse    |

### Products
| Method | Path              | Access | Description                                                      |
|--------|-------------------|--------|------------------------------------------------------------------|
| GET    | /products         | Any    | List with total balance (`?min_balance`, `?max_balance`, `?min_price`, `?max_price`, `?sort_by`, `?order`, `?limit`, `?offset`) |
| POST   | /products         | Admin  | Create product                                                   |
| GET    | /products/{id}    | Any    | Get product                                                      |
| PATCH  | /products/{id}    | Admin  | Update product                                                   |
| DELETE | /products/{id}    | Admin  | Soft-delete product                                              |

### Movements
| Method | Path              | Access | Description              |
|--------|-------------------|--------|--------------------------|
| POST   | /movements        | Any    | Create movement (IN/OUT/TRANSFER) |
| POST   | /movements/batch  | Any    | Batch movement by SKU list        |

## Architecture decisions

### Stock balance
Balance is never stored as a column. Every read computes it from `stock_movements`:
```
balance = SUM(quantity WHERE to_warehouse_id = X) - SUM(quantity WHERE from_warehouse_id = X)
```
`COALESCE(..., 0)` handles products with no movements yet.

### Race conditions
OUT and TRANSFER use `SELECT ... FOR UPDATE` on the product row before reading the balance. PostgreSQL holds a row-level lock for the duration of the transaction — concurrent requests queue up and each sees the already-updated balance. Batch operations sort products by `id` before locking to ensure all concurrent transactions acquire locks in the same order, preventing deadlocks.

### Batch operations
All items are validated in a single transaction. Balances for all products are fetched with one `GROUP BY` query after locking. If any item is short, the entire operation raises 409 and nothing is written:
```json
{"message": "Insufficient stock", "items": [{"sku": "...", "requested": 5, "available": 2}]}
```

### Aggregations
`GET /products` uses a subquery with `SUM(CASE WHEN type='IN' ...)` — one query for all products, no N+1. `GET /warehouses/valuation` uses a single `LEFT JOIN` with `SUM(CASE WHEN to_warehouse_id = X THEN qty*price WHEN from_warehouse_id = X THEN -qty*price)`.

### Cache
`GET /warehouses/valuation` result is cached in Redis under key `financial_valuation` with TTL 300s. The cache is invalidated (deleted) after every `commit` in movements, and after any product price or warehouse name change. If Redis is unavailable, the error is logged and the request proceeds normally.

### Idempotency-Key
`POST /movements` and `POST /movements/batch` accept an optional `Idempotency-Key` header. On the first request the key is atomically reserved in Redis with `SET NX` (status `processing`). After a successful commit the response (status code + body) is stored under `idempotency:{user_id}:{path}:{key}` for 24 hours. A repeat request with the same key returns the stored response without touching the database. If the first request is still in flight, the repeat gets 409. If the request fails (insufficient stock, 400, 404), the key is deleted so the client can retry after fixing the issue. If Redis is unavailable the request proceeds normally.

### Indexes and constraints
`stock_movements` has B-tree indexes on `product_id`, `from_warehouse_id`, and `to_warehouse_id` — the columns used in every balance aggregation query. A `CHECK (quantity > 0)` constraint enforces the business rule at the database level.

### Low-stock notification
After each OUT or TRANSFER `commit`, the new balance is compared to `min_stock_level`. If below, `BackgroundTasks.add_task` schedules `notify_low_stock` — the HTTP response is returned immediately and the notification runs after.

## Assumptions

- `TRANSFER` to the same warehouse is rejected at schema validation level (422).
- `performed_by` is always the authenticated user from the JWT token.
- Movements referencing inactive warehouses are rejected (400).
- Movements referencing soft-deleted products are rejected (404).
- Soft-deleted products are excluded from `GET /products` but their movement history is preserved.
- `GET /warehouses` hides inactive warehouses by default; pass `?include_inactive=true` to see all.

## Tests

| Test | What it verifies |
|------|-----------------|
| `test_race_condition_stock_never_goes_negative` | 15 concurrent OUT requests against stock of 10 — exactly 10 succeed, 5 get 409, final balance is 0 |
| `test_batch_atomicity_rollback_on_insufficient_stock` | Batch with one short item returns 409 and leaves all balances unchanged |
| `test_idempotency_key_prevents_duplicate_movement` | Two sequential OUT requests with the same `Idempotency-Key` create exactly one movement; second response matches the first |
| `test_balance_calculation_after_in_transfer_out` | IN(10) → TRANSFER(4) → OUT(3) yields total balance of 7; TRANSFER does not change the total |

## Not implemented / next steps

- **Sliding window rate limiting** — max 50 movements per minute per user via Redis sorted sets.
- Real email delivery (currently logs a warning).

## Running tests

Tests run against a real PostgreSQL instance from Docker Compose:

```bash
docker compose run --rm app pytest tests/ -v
```

## Project structure

```
app/
  config.py        # Settings from environment variables
  database.py      # Async engine and session factory
  models.py        # SQLAlchemy models
  schemas.py       # Pydantic schemas
  auth.py          # JWT and password utilities
  dependencies.py  # FastAPI dependencies (auth, roles)
  redis_client.py  # Redis client and cache invalidation
  notifications.py # Background low-stock notification
  routers/
    auth.py
    warehouses.py
    products.py
    movements.py
alembic/           # Database migrations
tests/
  conftest.py      # Fixtures (DB setup, client, auth)
  test_movements.py
```
