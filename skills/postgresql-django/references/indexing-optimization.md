# Indexing & Query Optimization

## Table of Contents
1. Index Types and Selection
2. EXPLAIN ANALYZE Workflow
3. VACUUM and Autovacuum
4. Query Planner Statistics
5. Performance Tuning Parameters
6. Django ORM Query Diagnosis

---

## 1. Index Types and Selection

### B-tree (Default)

Covers equality (`=`), range (`<`, `>`, `BETWEEN`), ordering (`ORDER BY`), and prefix matching (`LIKE 'abc%'`). Use for 90% of indexes.

```sql
CREATE INDEX idx_orders_customer ON orders (customer_id);
CREATE INDEX idx_orders_date ON orders (created_at DESC);

-- Composite: column order matters — most selective first for equality, range column last
CREATE INDEX idx_orders_status_date ON orders (status, created_at);

-- Partial index: only index what you query
CREATE INDEX idx_active_users ON users (email) WHERE is_active = true;

-- Covering index (INCLUDE): avoids heap access for covered columns
CREATE INDEX idx_orders_covering ON orders (customer_id) INCLUDE (total, status);
```

In Django:
```python
class Meta:
    indexes = [
        models.Index(fields=["customer", "created_at"], name="idx_order_cust_date"),
        models.Index(fields=["email"], condition=models.Q(is_active=True), name="idx_active_email"),
    ]
```

### GIN (Generalized Inverted Index)

For JSONB, arrays, full-text search, and hstore. Faster reads, slower writes (~2-3x insert overhead).

```python
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField

class Article(models.Model):
    metadata = models.JSONField(default=dict)
    tags = ArrayField(models.CharField(max_length=50))
    search_vector = SearchVectorField(null=True)

    class Meta:
        indexes = [
            GinIndex(fields=["metadata"], name="idx_article_metadata"),
            GinIndex(fields=["tags"], name="idx_article_tags"),
            GinIndex(fields=["search_vector"], name="idx_article_search"),
        ]
```

Use `jsonb_path_ops` for containment-only queries (smaller, faster):
```sql
CREATE INDEX idx_meta_pathops ON articles USING GIN (metadata jsonb_path_ops);
```

### BRIN (Block Range Index)

For large, physically ordered tables (time-series, append-only logs). **100-1000x smaller** than B-tree.

```python
from django.contrib.postgres.indexes import BrinIndex

class EventLog(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    data = models.JSONField()

    class Meta:
        indexes = [
            BrinIndex(fields=["created_at"], name="idx_event_brin"),
        ]
```

BRIN works because PostgreSQL stores data in 8KB pages. If `created_at` values are physically ordered (inserts are chronological), BRIN stores min/max per block range, allowing massive pruning. If data is frequently updated or inserted out of order, BRIN won't help — use B-tree instead.

### GiST (Generalized Search Tree)

For geometric data, range types, nearest-neighbor, and full-text with ranking.

```python
from django.contrib.postgres.indexes import GistIndex

class Reservation(models.Model):
    period = DateTimeRangeField()

    class Meta:
        indexes = [
            GistIndex(fields=["period"], name="idx_reservation_period"),
        ]
```

### Hash

Only for exact equality (`=`) on very high cardinality columns. Rarely better than B-tree in practice. Not crash-safe before PostgreSQL 10.

### Index Maintenance

Audit unused indexes regularly:
```sql
SELECT schemaname, indexrelname, idx_scan, pg_size_pretty(pg_relation_size(indexrelid)) AS size
FROM pg_stat_user_indexes
WHERE idx_scan = 0 AND indexrelname NOT LIKE '%_pkey'
ORDER BY pg_relation_size(indexrelid) DESC;
```

Detect index bloat:
```sql
SELECT tablename, indexname, pg_size_pretty(pg_relation_size(indexname::regclass)) AS index_size
FROM pg_indexes WHERE schemaname = 'public'
ORDER BY pg_relation_size(indexname::regclass) DESC LIMIT 20;
```

Rebuild bloated indexes without downtime:
```sql
REINDEX INDEX CONCURRENTLY idx_name;  -- PostgreSQL 12+
```

**Docs:** https://www.postgresql.org/docs/current/indexes-types.html

---

## 2. EXPLAIN ANALYZE Workflow

### Basic Usage

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) SELECT ...;
```

In Django:
```python
qs = Order.objects.filter(status="shipped", created_at__gt="2026-01-01").select_related("customer")
print(qs.explain(analyze=True, buffers=True))
```

### Red Flags to Look For

| Signal | Problem | Fix |
|--------|---------|-----|
| `Seq Scan` on large table | Missing index | Add appropriate index |
| `Rows Removed by Filter` >> rows returned | Index not covering filter | Add index or adjust existing |
| Estimated rows ≠ actual rows (10x+ off) | Stale statistics | Run `ANALYZE tablename` |
| `Sort Method: external merge Disk` | `work_mem` too low | Increase `work_mem` |
| `Nested Loop` with high row counts | Missing index on join column | Add index |
| `Hash Join` on small result set | Planner chose wrong strategy | Check statistics, consider `SET enable_hashjoin = off` temporarily for diagnosis |

### Query Plan Visualization Tools

- **explain.depesz.com** — paste EXPLAIN output, get visual breakdown (free)
- **explain.dalibo.com** — open-source, self-hostable
- **pgMustard** — commercial, gives ranked optimization advice
- **auto_explain** — log slow query plans automatically in production

```sql
-- Enable auto_explain for queries > 200ms
ALTER SYSTEM SET auto_explain.log_min_duration = '200ms';
ALTER SYSTEM SET auto_explain.log_analyze = on;
ALTER SYSTEM SET auto_explain.log_buffers = on;
SELECT pg_reload_conf();
```

### pg_stat_statements

Essential for production. Tracks query statistics (time, calls, rows, buffers).

```sql
-- Top queries by total time
SELECT query, calls,
       round(total_exec_time::numeric, 2) AS total_ms,
       round(mean_exec_time::numeric, 2) AS mean_ms,
       round((stddev_exec_time)::numeric, 2) AS stddev_ms,
       rows
FROM pg_stat_statements
ORDER BY total_exec_time DESC LIMIT 20;

-- Reset stats periodically
SELECT pg_stat_statements_reset();
```

**Docs:** https://www.postgresql.org/docs/current/pgstatstatements.html

---

## 3. VACUUM and Autovacuum

PostgreSQL uses MVCC: UPDATE/DELETE creates "dead tuples" that occupy space until vacuumed.

### Autovacuum Tuning for Production

Default settings are too conservative. Recommended baseline:

```sql
-- postgresql.conf or ALTER SYSTEM
ALTER SYSTEM SET autovacuum_vacuum_scale_factor = 0.05;     -- default 0.2
ALTER SYSTEM SET autovacuum_analyze_scale_factor = 0.02;    -- default 0.1
ALTER SYSTEM SET autovacuum_vacuum_cost_limit = 1000;       -- default 200
ALTER SYSTEM SET autovacuum_max_workers = 4;                -- default 3
SELECT pg_reload_conf();
```

For high-traffic tables, set per-table thresholds:
```sql
ALTER TABLE events SET (
    autovacuum_vacuum_scale_factor = 0,
    autovacuum_vacuum_threshold = 10000,
    autovacuum_analyze_scale_factor = 0,
    autovacuum_analyze_threshold = 5000
);
```

### Monitoring VACUUM

```sql
-- Tables that need vacuuming
SELECT relname, n_dead_tup, n_live_tup,
       round(n_dead_tup::numeric / NULLIF(n_live_tup, 0) * 100, 1) AS dead_pct,
       last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
WHERE n_dead_tup > 1000
ORDER BY n_dead_tup DESC;
```

### VACUUM FULL vs REINDEX CONCURRENTLY

- `VACUUM` (regular): marks dead tuples as reusable, no lock, no space reclaimed to OS
- `VACUUM FULL`: rewrites entire table, **ACCESS EXCLUSIVE lock** — only for severe bloat
- `REINDEX CONCURRENTLY`: rebuilds indexes without locking — preferred for index bloat

**Docs:** https://www.postgresql.org/docs/current/routine-vacuuming.html

---

## 4. Query Planner Statistics

The planner uses statistics to estimate row counts and choose execution plans.

### ANALYZE

Run after bulk loads or when estimated vs actual rows diverge:
```sql
ANALYZE tablename;                    -- single table
ANALYZE;                              -- entire database (slow)
```

### Extended Statistics

When columns are correlated (city + zip_code, country + language):
```sql
CREATE STATISTICS stat_addr_city_zip (dependencies, ndistinct, mcv)
    ON city, zip_code FROM addresses;
ANALYZE addresses;
```

Types:
- `dependencies`: functional dependencies between columns
- `ndistinct`: distinct value counts for combinations (helps GROUP BY)
- `mcv` (PostgreSQL 12+): most common value combinations

In Django, create with RunSQL in migrations:
```python
migrations.RunSQL(
    "CREATE STATISTICS stat_order_status_date (dependencies) ON status, created_at FROM orders;",
    "DROP STATISTICS IF EXISTS stat_order_status_date;",
)
```

### Adjusting Statistics Target

For columns with irregular distributions:
```sql
ALTER TABLE orders ALTER COLUMN status SET STATISTICS 500;  -- default 100
ANALYZE orders;
```

Higher target = more accurate estimates but more time to ANALYZE and more memory.

**Docs:** https://www.postgresql.org/docs/current/planner-stats.html, https://www.postgresql.org/docs/current/sql-createstatistics.html

---

## 5. Performance Tuning Parameters

### Memory Configuration

| Parameter | Recommendation | Notes |
|-----------|---------------|-------|
| `shared_buffers` | 25% of RAM | PostgreSQL's buffer cache |
| `effective_cache_size` | 75% of RAM | Hint to planner about OS cache |
| `work_mem` | 64MB (start) | Per-sort/hash operation, not per-connection |
| `maintenance_work_mem` | 512MB-1GB | VACUUM, CREATE INDEX, REINDEX |

### I/O Configuration (SSDs)

```sql
ALTER SYSTEM SET random_page_cost = 1.1;          -- default 4.0 (spinning disks)
ALTER SYSTEM SET effective_io_concurrency = 200;   -- default 1
ALTER SYSTEM SET seq_page_cost = 1.0;              -- keep default
```

### WAL Configuration

```sql
ALTER SYSTEM SET wal_buffers = '64MB';
ALTER SYSTEM SET checkpoint_completion_target = 0.9;
ALTER SYSTEM SET max_wal_size = '2GB';
```

### Connection Configuration

```sql
ALTER SYSTEM SET max_connections = 200;  -- with connection pooling, 100-200 is typical
ALTER SYSTEM SET idle_in_transaction_session_timeout = '30s';
ALTER SYSTEM SET statement_timeout = '30s';  -- prevent runaway queries
```

Use **pgTune** (https://pgtune.leopard.in.ua/) to generate a baseline config for your hardware.

### Driver-Level Optimizations (psycopg3)

psycopg3 provides additional performance features at the driver level:
- **Prepared statements** (auto, 10-30% faster for repeated queries)
- **Pipeline mode** (20-25x faster for batch operations)
- **Binary protocol** (2-5x faster for bytea, UUID, large arrays)
- **COPY protocol** (10-100x faster than INSERT for bulk loading)

See `references/django-orm.md` Section 5 for configuration details.

**Docs:** https://www.postgresql.org/docs/current/runtime-config.html

---

## 6. Django ORM Query Diagnosis

### Detecting N+1 with django-debug-toolbar

Install and check the SQL panel during development. Every page load should show a small, predictable number of queries.

### Using Django's QuerySet.explain()

```python
# In Django shell
from myapp.models import Order
qs = Order.objects.filter(status="active").select_related("customer")
print(qs.explain(analyze=True, buffers=True, verbose=True))
```

### Common ORM Pitfalls

```python
# BAD: N+1 — each order triggers a query for customer
for order in Order.objects.all():
    print(order.customer.name)

# GOOD: 1 query with JOIN
for order in Order.objects.select_related("customer"):
    print(order.customer.name)

# BAD: N+1 on reverse FK / M2M
for author in Author.objects.all():
    print(author.books.count())

# GOOD: 2 queries (1 for authors, 1 for books)
for author in Author.objects.prefetch_related("books"):
    print(author.books.count())

# BEST: Single query with annotation
from django.db.models import Count
authors = Author.objects.annotate(book_count=Count("books"))
for author in authors:
    print(author.book_count)
```

### Efficient Aggregations

```python
from django.db.models import F, Sum, Subquery, OuterRef

# Use F() for database-level calculations
Order.objects.update(total=F("subtotal") + F("tax"))

# Use Subquery for correlated calculations
latest_order = Order.objects.filter(customer=OuterRef("pk")).order_by("-created_at")
customers = Customer.objects.annotate(
    last_order_date=Subquery(latest_order.values("created_at")[:1])
)

# Use exists() instead of count() > 0
if Order.objects.filter(customer_id=1).exists():  # GOOD
    pass
```
