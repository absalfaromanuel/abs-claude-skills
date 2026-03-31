# Django ORM + PostgreSQL Optimizado

## Table of Contents
1. psycopg3: Installation, Configuration & Migration
2. PostgreSQL-Specific Fields
3. Eliminating N+1 Queries
4. Zero-Downtime Migrations
5. Connection Pooling & psycopg3 Advanced Features
6. Essential Extensions

---

## 1. psycopg3: Installation, Configuration & Migration

### Installation Variants

The package name is `psycopg` (NOT `psycopg3`). Three variants determine performance:

```bash
# Production (compiled C extension, uses system libpq):
pip install "psycopg[c,pool]"
# Requires: apt install python3-dev libpq-dev

# Development (bundled libpq, no compiler needed):
pip install "psycopg[binary,pool]"

# Verify installed implementation:
python -c "import psycopg; print(psycopg.pq.__impl__)"  # 'c', 'binary', or 'python'
```

`psycopg[c]` is recommended for production — system security updates apply automatically.
`psycopg[binary]` bundles its own libpq (may lag behind security patches).
Pure `psycopg` (no extras) uses ctypes — significantly slower, only for debugging.

### Django Configuration (4.2+)

Django auto-detects psycopg3 — same `ENGINE`, just install `psycopg` and uninstall `psycopg2`:

```python
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",  # Same for psycopg2 and psycopg3
        "NAME": os.environ["DB_NAME"],
        "USER": os.environ["DB_USER"],
        "PASSWORD": os.environ["DB_PASSWORD"],
        "HOST": os.environ["DB_HOST"],
        "PORT": os.environ.get("DB_PORT", "5432"),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "pool": {  # Django 5.1+ native pooling (see Section 5)
                "min_size": 2,
                "max_size": 4,
                "timeout": 10,
                "max_lifetime": 1800,
            },
        },
    }
}
```

### Optimize Per-Role Settings

Avoid Django setting these per-connection (saves 1 round-trip per connect):
```sql
ALTER ROLE django_app SET client_encoding TO 'UTF8';
ALTER ROLE django_app SET default_transaction_isolation TO 'read committed';
ALTER ROLE django_app SET timezone TO 'UTC';
```

### Migration from psycopg2: Breaking Changes

psycopg3 uses **server-side parameter binding** by default — queries and parameters are sent
separately to PostgreSQL. This breaks several common patterns:

```python
# ❌ BROKEN: Can't parameterize non-DML statements
conn.execute("SET TimeZone TO %s", ["UTC"])
# ✅ FIX: Use function form
conn.execute("SELECT set_config('TimeZone', %s, false)", ["UTC"])

# ❌ BROKEN: IN with tuple
conn.execute("SELECT * FROM foo WHERE id IN %s", [(10, 20, 30)])
# ✅ FIX: Use ANY() with list
conn.execute("SELECT * FROM foo WHERE id = ANY(%s)", [[10, 20, 30]])

# ❌ BROKEN: NOTIFY with parameters
conn.execute("NOTIFY %s, %s", ["channel", "payload"])
# ✅ FIX: Use pg_notify function
conn.execute("SELECT pg_notify(%s, %s)", ["channel", "payload"])
```

Other key changes:
- `with conn` now **CLOSES** the connection (psycopg2 only committed). Use `with conn.transaction()` for transaction management.
- `RealDictCursor` → `row_factory=dict_row`. `NamedTupleCursor` → `row_factory=namedtuple_row`.
- `mogrify()` not available on standard cursors (use `ClientCursor` for debugging).
- `copy_from()`/`copy_to()` replaced by unified `copy()` method (see `partitioning-sharding.md`).

### Row Factories

Control how rows are returned — set per-connection or per-cursor:

```python
from psycopg.rows import dict_row, namedtuple_row, class_row, scalar_row
from dataclasses import dataclass

# dict_row — ideal for API responses
conn = psycopg.connect(DSN, row_factory=dict_row)

# class_row — type-safe domain objects
@dataclass
class Employee:
    id: int
    name: str
    salary: float

cur = conn.cursor(row_factory=class_row(Employee))
cur.execute("SELECT id, name, salary FROM employees WHERE id = %s", [1])
emp = cur.fetchone()  # → Employee(id=1, name='Carlos', salary=500.0)

# scalar_row — for single-value queries
cur = conn.cursor(row_factory=scalar_row)
count = cur.execute("SELECT count(*) FROM employees").fetchone()  # → 42
```

Use `tuple_row` (default) for hot paths; `dict_row` or `class_row` for API layers.

### Type Adaptation (JSONB, Ranges, Custom Types)

psycopg3 uses Dumpers (Python→PG) and Loaders (PG→Python):

```python
from psycopg.types.json import Jsonb, set_json_dumps, set_json_loads
import orjson  # faster JSON library

# JSONB requires explicit wrapper in raw SQL:
conn.execute("INSERT INTO events (data) VALUES (%s)", [Jsonb({"action": "click"})])

# Use faster JSON library globally:
set_json_dumps(lambda obj: orjson.dumps(obj).decode())
set_json_loads(orjson.loads)

# Django's JSONField handles this automatically — no wrapper needed in ORM
```

**Docs:** https://www.psycopg.org/psycopg3/docs/basic/install.html, https://www.psycopg.org/psycopg3/docs/basic/from_pg2.html, https://docs.djangoproject.com/en/6.0/ref/databases/

---

## 2. PostgreSQL-Specific Fields

Django's `django.contrib.postgres` module exposes PostgreSQL-native types:

```python
from django.contrib.postgres.fields import (
    ArrayField, HStoreField, JSONField,
    DateTimeRangeField, IntegerRangeField,
)
from django.contrib.postgres.search import SearchVectorField
from django.contrib.postgres.indexes import GinIndex, BrinIndex, GistIndex
from django.contrib.postgres.constraints import ExclusionConstraint

class Product(models.Model):
    # JSONB — semi-structured data with GIN indexing
    metadata = models.JSONField(default=dict)
    
    # Array — small, fixed-type lists
    tags = ArrayField(models.CharField(max_length=50), default=list, blank=True)
    
    # Full-text search vector
    search_vector = SearchVectorField(null=True)
    
    class Meta:
        indexes = [
            GinIndex(fields=["metadata"], name="idx_product_metadata"),
            GinIndex(fields=["tags"], name="idx_product_tags"),
            GinIndex(fields=["search_vector"], name="idx_product_search"),
        ]


class Reservation(models.Model):
    room = models.ForeignKey("Room", on_delete=models.CASCADE)
    period = DateTimeRangeField()
    
    class Meta:
        indexes = [
            GistIndex(fields=["period"], name="idx_reservation_period"),
        ]
        constraints = [
            ExclusionConstraint(
                name="prevent_room_overlap",
                expressions=[
                    ("room", RangeOperators.EQUAL),
                    ("period", RangeOperators.OVERLAPS),
                ],
            ),
        ]
```

### Full-Text Search

```python
from django.contrib.postgres.search import (
    SearchVector, SearchQuery, SearchRank, TrigramSimilarity,
)

# Basic search
results = Article.objects.annotate(
    search=SearchVector("title", "body", config="spanish"),
).filter(search=SearchQuery("base datos", config="spanish"))

# Ranked search
vector = SearchVector("title", weight="A") + SearchVector("body", weight="B")
query = SearchQuery("postgresql optimización", config="spanish")
results = Article.objects.annotate(
    rank=SearchRank(vector, query)
).filter(rank__gte=0.1).order_by("-rank")

# Trigram similarity (fuzzy matching) — requires pg_trgm extension
results = Article.objects.annotate(
    similarity=TrigramSimilarity("title", "postgre"),
).filter(similarity__gt=0.3).order_by("-similarity")
```

Enable required extensions in migrations:
```python
from django.contrib.postgres.operations import TrigramExtension, UnaccentExtension

class Migration(migrations.Migration):
    operations = [
        TrigramExtension(),
        UnaccentExtension(),
    ]
```

**Docs:** https://docs.djangoproject.com/en/5.2/ref/contrib/postgres/fields/, https://docs.djangoproject.com/en/5.2/ref/contrib/postgres/search/

---

## 3. Eliminating N+1 Queries

### select_related (SQL JOIN)

For ForeignKey and OneToOneField. Produces a single query with JOIN:

```python
# BAD: 1 + N queries
orders = Order.objects.all()
for order in orders:
    print(order.customer.name)  # each access = 1 query

# GOOD: 1 query with JOIN
orders = Order.objects.select_related("customer")
for order in orders:
    print(order.customer.name)  # no extra query

# Chain for nested relations
orders = Order.objects.select_related("customer__company__industry")
```

### prefetch_related (separate query + Python join)

For ManyToManyField and reverse ForeignKey. Produces 2+ queries:

```python
# BAD: 1 + N queries
authors = Author.objects.all()
for author in authors:
    print(author.books.all())  # each access = 1 query

# GOOD: 2 queries
authors = Author.objects.prefetch_related("books")
for author in authors:
    print(author.books.all())  # cached, no extra query
```

### Prefetch with Custom Querysets

For filtering, ordering, or annotating prefetched data:

```python
from django.db.models import Prefetch

authors = Author.objects.prefetch_related(
    Prefetch(
        "books",
        queryset=Book.objects.filter(published=True)
                    .select_related("publisher")
                    .order_by("-publish_date")[:5],
        to_attr="recent_books",  # stores as list, not queryset
    )
)
for author in authors:
    for book in author.recent_books:  # list, already filtered
        print(book.publisher.name)     # no extra query
```

### defer() and only()

Limit which columns are loaded:

```python
# Only load specific columns
orders = Order.objects.select_related("customer").only(
    "id", "total", "status", "customer__name"
)

# Defer heavy columns
articles = Article.objects.defer("body", "metadata")
```

### Bulk Operations

```python
# Bulk create (1 query instead of N)
Book.objects.bulk_create([
    Book(title=f"Book {i}", author=author)
    for i in range(1000)
], batch_size=500)

# Bulk update
books = Book.objects.filter(status="draft")
for book in books:
    book.status = "published"
Book.objects.bulk_update(books, ["status"], batch_size=500)

# update() for simple field changes (1 query)
Book.objects.filter(status="draft").update(status="published")
```

### Annotations and Aggregations

```python
from django.db.models import Count, Sum, Avg, F, Q, Subquery, OuterRef, Value
from django.db.models.functions import Coalesce

# Annotate with counts
authors = Author.objects.annotate(
    book_count=Count("books"),
    published_count=Count("books", filter=Q(books__published=True)),
)

# Subquery for correlated data
latest_order = Order.objects.filter(
    customer=OuterRef("pk")
).order_by("-created_at")

customers = Customer.objects.annotate(
    last_order_total=Subquery(latest_order.values("total")[:1]),
    last_order_date=Subquery(latest_order.values("created_at")[:1]),
)

# F expressions for database-level calculations
Order.objects.update(total_with_tax=F("total") * 1.16)

# exists() instead of count() > 0
if Order.objects.filter(customer=customer).exists():
    pass

# iterator() for large datasets
for order in Order.objects.filter(status="active").iterator(chunk_size=2000):
    process(order)
```

**Docs:** https://docs.djangoproject.com/en/5.2/topics/db/optimization/

---

## 4. Zero-Downtime Migrations

### Dangerous Operations

| Operation | Risk | Safe Alternative |
|-----------|------|-----------------|
| `CREATE INDEX` | Blocks writes | `CREATE INDEX CONCURRENTLY` |
| `ADD COLUMN NOT NULL DEFAULT x` | Rewrites table (PG < 11) | Add nullable → backfill → add constraint |
| `ALTER COLUMN TYPE` | Rewrites table | Create new column → migrate data → swap |
| `RENAME COLUMN/TABLE` | Breaks code during deploy | Use db_column, deploy in phases |

### Concurrent Index Creation

```python
class Migration(migrations.Migration):
    atomic = False  # REQUIRED for CONCURRENTLY

    operations = [
        migrations.RunSQL(
            "CREATE INDEX CONCURRENTLY idx_order_status ON orders (status);",
            "DROP INDEX CONCURRENTLY idx_order_status;",
        ),
    ]
```

### Safe NOT NULL Addition

```python
# Migration 1: Add nullable column
migrations.AddField(
    model_name="order",
    name="priority",
    field=models.IntegerField(null=True),
),

# Migration 2: Backfill in batches (RunPython)
def backfill_priority(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    batch_size = 5000
    while Order.objects.filter(priority__isnull=True).exists():
        ids = list(
            Order.objects.filter(priority__isnull=True)
            .values_list("id", flat=True)[:batch_size]
        )
        Order.objects.filter(id__in=ids).update(priority=0)

# Migration 3: Add NOT NULL constraint
migrations.RunSQL(
    "ALTER TABLE orders ALTER COLUMN priority SET NOT NULL;",
    "ALTER TABLE orders ALTER COLUMN priority DROP NOT NULL;",
)
```

### django-pg-zero-downtime-migrations

Automatically rewrites unsafe operations:
```python
# settings.py
INSTALLED_APPS = ["django_zero_downtime_migrations", ...]
ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE = True  # fail in CI
```

**Library:** https://github.com/tbicr/django-pg-zero-downtime-migrations

### Pre-Production Checklist

Always run before deploying migrations:
```bash
python manage.py sqlmigrate myapp 0042  # inspect actual SQL
python manage.py migrate --plan          # see migration order
```

---

## 5. Connection Pooling & psycopg3 Advanced Features

This is the **single reference** for all pooling and driver-level performance topics.

### Django 5.1+ Native Pooling (recommended)

Uses `psycopg_pool.ConnectionPool` internally. `CONN_MAX_AGE` **must** be `0`.

```python
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "CONN_MAX_AGE": 0,       # Required — Django raises ImproperlyConfigured otherwise
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "pool": {
                "min_size": 2,       # Warm connections per worker
                "max_size": 4,       # Max per worker process
                "timeout": 10,       # Seconds to wait for connection
                "max_lifetime": 1800, # Recycle every 30 min
                "max_idle": 300,     # Close idle connections after 5 min
            },
        },
    }
}
```

**Sizing formula**: `total_connections = servers × gunicorn_workers × max_size`.
Example: 2 servers × 4 workers × 4 max_size = **32 connections**.
Must be less than PostgreSQL `max_connections` (default 100). Reserve ~10 for admin/migrations.

The pool is **per-process** — each Gunicorn worker has its own pool.

### PgBouncer (for multi-server, Django < 5.1, or ASGI)

External proxy that centralizes connection pooling across multiple app servers.

```python
# Django settings for PgBouncer
DATABASES = {
    "default": {
        "HOST": "pgbouncer.internal",
        "PORT": "6432",
        "CONN_MAX_AGE": 0,
        "DISABLE_SERVER_SIDE_CURSORS": True,  # Required for transaction mode
        "OPTIONS": {
            "prepare_threshold": None,  # Disable prepared statements (PgBouncer < 1.22)
        },
    }
}
```

Run migrations directly against PostgreSQL, not through PgBouncer.

### Decision Guide: Native Pool vs PgBouncer

| Aspect | Django Native Pool | PgBouncer |
|---|---|---|
| Architecture | In-process (per worker) | External proxy (centralized) |
| Setup | Zero (just settings) | Separate service |
| Multi-server | No (each server pools independently) | Yes (single pool for all servers) |
| Prepared statements | Yes (auto) | Only PgBouncer ≥ 1.22 |
| Server-side cursors | Yes | No (transaction mode) |
| Best for | Single server, < 1000 req/s | Multiple servers, high volume |

### Prepared Statements (automatic in psycopg3)

psycopg3 auto-prepares queries after `prepare_threshold=5` executions (LRU cache of 100 statements).
**10-30% faster** for repeated queries. Django benefits automatically.

```python
# Disable for PgBouncer transaction mode (< 1.22):
"OPTIONS": {"prepare_threshold": None}

# Force immediate preparation for hot queries (raw SQL only):
cursor.execute("SELECT * FROM users WHERE tenant_id = %s", [tid], prepare=True)
```

### Server-Side Cursors and `.iterator()`

Server-side cursors fetch rows in batches — essential for large exports:

```python
# Django's .iterator() uses server-side cursors automatically:
for obj in MyModel.objects.filter(tenant_id=tid).iterator(chunk_size=2000):
    process(obj)
```

**PgBouncer incompatibility**: In transaction mode, the connection can change between fetches.
Use `DISABLE_SERVER_SIDE_CURSORS = True` in Django, or route heavy exports through a direct connection:

```python
DATABASES = {
    "default": {"HOST": "pgbouncer", "DISABLE_SERVER_SIDE_CURSORS": True},
    "direct": {"HOST": "postgres_direct", "DISABLE_SERVER_SIDE_CURSORS": False},
}
# Route heavy exports through direct connection:
for obj in MyModel.objects.using("direct").all().iterator(chunk_size=2000):
    export(obj)
```

### Pipeline Mode (psycopg 3.1+)

Sends multiple queries without waiting for individual results. **20-25x faster** for batch operations:

```python
# Access via raw connection in Django:
raw_conn = connection.connection
with raw_conn.pipeline():
    for sql_query, params in batch_operations:
        raw_conn.execute(sql_query, params)
```

Note: `executemany()` already uses pipeline internally since psycopg 3.1.

**Docs:** https://www.psycopg.org/psycopg3/docs/advanced/pool.html, https://www.psycopg.org/psycopg3/docs/advanced/prepare.html, https://www.pgbouncer.org/config.html

---

## 6. Essential Extensions

### django-pghistory

Automatic audit trails via PostgreSQL triggers:

```python
import pghistory

@pghistory.track(
    pghistory.Snapshot(),
    pghistory.AfterUpdate(condition=pghistory.AnyChange("status")),
)
class Order(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20)
```

**Library:** https://github.com/AmbitionEng/django-pghistory

### django-pgtrigger

Define PostgreSQL triggers from Django:

```python
import pgtrigger

@pgtrigger.register(
    pgtrigger.Protect(name="protect_deletion", operation=pgtrigger.Delete),
    pgtrigger.SoftDelete(name="soft_delete", field="is_deleted"),
)
class Document(models.Model):
    title = models.CharField(max_length=200)
    is_deleted = models.BooleanField(default=False)
```

**Library:** https://github.com/AmbitionEng/django-pgtrigger

### django-postgres-extra

Adds upserts, partitioning, and PostgreSQL-specific operations:

```python
from psqlextra.query import ConflictAction

MyModel.objects.on_conflict(["unique_field"], ConflictAction.UPDATE).bulk_insert([
    {"unique_field": "a", "value": 1},
    {"unique_field": "b", "value": 2},
])
```

**Library:** https://github.com/SectorLabs/django-postgres-extra

### django-ltree

Efficient hierarchical data using PostgreSQL's `ltree` type:

```python
from django_ltree.fields import PathField

class Category(models.Model):
    name = models.CharField(max_length=100)
    path = PathField()  # stores like "root.electronics.phones"
```

**Library:** https://github.com/mariocesar/django-ltree

### django-pg-zero-downtime-migrations

Safe migrations in production:
```python
ZERO_DOWNTIME_MIGRATIONS_RAISE_FOR_UNSAFE = True
ZERO_DOWNTIME_MIGRATIONS_LOCK_TIMEOUT = "2s"
ZERO_DOWNTIME_MIGRATIONS_STATEMENT_TIMEOUT = "2s"
```

**Library:** https://github.com/tbicr/django-pg-zero-downtime-migrations
