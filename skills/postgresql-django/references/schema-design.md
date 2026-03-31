# Schema Design & Normalization

## Table of Contents
1. Normalization (1NF → BCNF)
2. When to Desnormalize
3. Naming Conventions
4. PostgreSQL Data Types
5. Multi-Tenant Architecture
6. Materialized Views

---

## 1. Normalization

Normalize to 3NF by default. Only go further (BCNF) when you encounter specific anomalies.

**1NF**: Every column holds atomic values; a primary key exists.
**2NF**: No partial dependencies on composite keys (every non-key column depends on the full PK).
**3NF**: No transitive dependencies (non-key columns don't depend on other non-key columns).
**BCNF**: Every determinant is a candidate key (handles edge cases 3NF misses).

Django's ORM naturally enforces 1NF-2NF through model design. The main risk is 3NF violations — storing derived or redundant data in the same table.

```python
# BAD: 3NF violation — category_description depends on category, not on product
class Product(models.Model):
    name = models.CharField(max_length=100)
    category = models.CharField(max_length=50)
    category_description = models.TextField()  # transitive dependency

# GOOD: normalized
class Category(models.Model):
    name = models.CharField(max_length=50, unique=True)
    description = models.TextField(blank=True)

class Product(models.Model):
    name = models.CharField(max_length=100)
    category = models.ForeignKey(Category, on_delete=models.PROTECT)
```

**Docs:** https://www.postgresql.org/docs/current/ddl.html, https://www.postgresql.org/docs/current/ddl-constraints.html

---

## 2. When to Desnormalize

Desnormalize only when EXPLAIN ANALYZE proves a specific JOIN is a bottleneck. Common valid cases:

- **Read:write ratio > 10:1** and you need sub-100ms responses
- **Dashboard aggregations** that scan millions of rows
- **API endpoints with strict SLA** latency requirements

### Strategy 1: Materialized Views (preferred)

Pre-compute expensive queries. Refresh without blocking readers:

```sql
CREATE MATERIALIZED VIEW sales_summary AS
SELECT seller_id, date_trunc('month', sold_at) AS month,
       SUM(amount) AS total, COUNT(*) AS num_sales
FROM sales GROUP BY 1, 2;

CREATE UNIQUE INDEX ON sales_summary (seller_id, month);
REFRESH MATERIALIZED VIEW CONCURRENTLY sales_summary;
```

Expose in Django as unmanaged model:

```python
class SalesSummary(models.Model):
    seller_id = models.IntegerField()
    month = models.DateField()
    total = models.DecimalField(max_digits=13, decimal_places=2)
    num_sales = models.IntegerField()

    class Meta:
        managed = False
        db_table = "sales_summary"
```

### Strategy 2: Counter Caches

Store counts in the parent row, updated by triggers or Django signals:

```python
class Author(models.Model):
    name = models.CharField(max_length=100)
    book_count = models.PositiveIntegerField(default=0)  # denormalized

# Keep in sync with a trigger (preferred) or post_save signal
```

### Strategy 3: JSONB Denormalization

Store a snapshot of related data for read-heavy access:

```python
class Order(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    customer_snapshot = models.JSONField(default=dict)  # {name, email, address}
    # Populate on save; the FK is the source of truth
```

**Rule:** Normalized tables are the source of truth. Denormalized pieces are read-optimized copies you can always rebuild.

**Docs:** https://www.postgresql.org/docs/current/rules-materializedviews.html

---

## 3. Naming Conventions

PostgreSQL folds unquoted identifiers to lowercase. Always use `snake_case`.

| Element | Convention | Example |
|---------|-----------|---------|
| Tables | plural, snake_case | `users`, `order_items` |
| Columns | snake_case | `first_name`, `created_at` |
| Primary keys | `id` or `{singular}_id` | `id`, `user_id` |
| Foreign keys | `{referenced_singular}_id` | `customer_id`, `tenant_id` |
| Booleans | `is_`, `has_`, `can_` prefix | `is_active`, `has_paid` |
| Timestamps | `_at` suffix | `created_at`, `deleted_at` |
| Constraints | `{table}_{col}_{type}` | `users_email_unique` |
| Indexes | `idx_{table}_{cols}` | `idx_orders_customer_created` |

Django auto-generates table names as `{app_label}_{model_name}` — this aligns well.
Identifier max length: **63 characters** — longer names are silently truncated.

**Docs:** https://www.postgresql.org/docs/current/sql-syntax-lexical.html#SQL-SYNTAX-IDENTIFIERS

---

## 4. PostgreSQL Data Types

Choose the most specific type available. Avoid generic VARCHAR for everything.

### Primary Keys

```python
# Internal PK: BIGINT identity (8 bytes, sequential, B-tree friendly)
id = models.BigAutoField(primary_key=True)

# External/API PK: UUID (16 bytes, no information leakage)
uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
```

Use UUIDv7 (PostgreSQL 17+, `uuidv7()`) for time-sortable UUIDs that don't fragment B-tree indexes. For PostgreSQL < 17, use UUIDv4 with a separate sequential PK.

### Text

- `VARCHAR(n)`: when there's a natural maximum (email: 254, phone: 20)
- `TEXT`: when no natural maximum exists (descriptions, notes)
- `CITEXT`: case-insensitive text (emails, usernames) — via `CREATE EXTENSION citext`

### Numeric

- `INTEGER` / `BIGINT`: whole numbers (use BIGINT for IDs, counters that may exceed 2B)
- `NUMERIC(p,s)`: exact decimals (money, financial calculations)
- `REAL` / `DOUBLE PRECISION`: approximate floating point (scientific, non-financial)
- **Never use FLOAT for money.** Use `NUMERIC` or store cents as `BIGINT`.

### Date/Time

- **Always `TIMESTAMPTZ`** (never `TIMESTAMP` without timezone)
- `DATE`: when you only need the date
- `INTERVAL`: durations
- `TSRANGE` / `DATERANGE`: periods with exclusion constraints

### JSON

- **Always `JSONB`** (never `JSON`) — binary storage, indexable with GIN, supports containment (`@>`)
- Promote frequently-queried keys to typed columns
- Django: `models.JSONField()` maps to JSONB by default

### Arrays

```python
from django.contrib.postgres.fields import ArrayField
tags = ArrayField(models.CharField(max_length=50), default=list)
```

Good for small, fixed-type lists. For complex relationships, prefer a proper M2M table.

### Range Types

```python
from django.contrib.postgres.fields import DateTimeRangeField
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeOperators

class Reservation(models.Model):
    room = models.ForeignKey(Room, on_delete=models.CASCADE)
    period = DateTimeRangeField()

    class Meta:
        constraints = [
            ExclusionConstraint(
                name="prevent_room_overlap",
                expressions=[("room", RangeOperators.EQUAL), ("period", RangeOperators.OVERLAPS)],
            ),
        ]
```

**Docs:** https://www.postgresql.org/docs/current/datatype.html

---

## 5. Multi-Tenant Architecture

### Option A: Schema-Based (`django-tenants`)

Each tenant gets a PostgreSQL schema. Strong isolation without `WHERE tenant_id` in every query.

- **Pros:** Clean isolation, no risk of data leakage, per-tenant schema customization possible
- **Cons:** Migrations run per-schema (slow with many tenants), `pg_catalog` bloats beyond ~500 schemas
- **Best for:** < 500 tenants with strong isolation requirements
- **Library:** https://github.com/django-tenants/django-tenants

### Option B: Shared Schema + RLS (recommended for most SaaS)

Single schema, `tenant_id` column on every table, Row Level Security policies enforce isolation.

- **Pros:** Standard migrations, scales to millions of tenants, Citus-compatible for sharding
- **Cons:** Must ensure `tenant_id` is always set, RLS policies must be carefully tested
- **Best for:** Most SaaS applications
- **Library:** https://github.com/citusdata/django-multitenant

Implementation pattern:

```python
# Middleware sets tenant context per request
class TenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant_id = get_tenant_from_request(request)
        with connection.cursor() as cursor:
            # SET LOCAL scopes to current transaction (not session!)
            cursor.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        return self.get_response(request)
```

```sql
-- RLS policy
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON projects
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);
```

**Critical:** Always include `tenant_id` as the first column in composite indexes:
```python
class Meta:
    indexes = [
        models.Index(fields=["tenant", "status", "created_at"]),
    ]
```

### Option C: Hybrid (Schema + RLS for Enterprise Tenants)

Shared schema with RLS for standard tenants; dedicated schemas for enterprise tenants that need compliance isolation.

**Docs (AWS):** https://docs.aws.amazon.com/prescriptive-guidance/latest/saas-multitenant-managed-postgresql/partitioning-models.html
**Docs (Supabase RLS):** https://supabase.com/docs/guides/database/postgres/row-level-security
