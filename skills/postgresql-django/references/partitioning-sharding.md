# Partitioning & Sharding for Large Tables

## Table of Contents
1. Declarative Partitioning
2. Partition Management with pg_partman
3. Django Integration
4. Sharding with Citus
5. Keyset Pagination
6. Archiving & Data Lifecycle

---

## 1. Declarative Partitioning

PostgreSQL 10+ supports native declarative partitioning. The partition key MUST be part of the primary key.

### Range Partitioning (most common)

Ideal for time-series data, logs, events, audit trails:

```sql
CREATE TABLE events (
    id BIGSERIAL,
    tenant_id UUID NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    payload JSONB,
    PRIMARY KEY (id, event_time)  -- partition key in PK
) PARTITION BY RANGE (event_time);

-- Create partitions
CREATE TABLE events_2026_q1 PARTITION OF events
    FOR VALUES FROM ('2026-01-01') TO ('2026-04-01');
CREATE TABLE events_2026_q2 PARTITION OF events
    FOR VALUES FROM ('2026-04-01') TO ('2026-07-01');
CREATE TABLE events_2026_q3 PARTITION OF events
    FOR VALUES FROM ('2026-07-01') TO ('2026-10-01');
CREATE TABLE events_2026_q4 PARTITION OF events
    FOR VALUES FROM ('2026-10-01') TO ('2027-01-01');

-- Always create a DEFAULT partition for unexpected data
CREATE TABLE events_default PARTITION OF events DEFAULT;
```

### List Partitioning

For categorical data (regions, tenant tiers, document types):

```sql
CREATE TABLE documents (
    id BIGSERIAL,
    doc_type VARCHAR(20) NOT NULL,
    content JSONB,
    PRIMARY KEY (id, doc_type)
) PARTITION BY LIST (doc_type);

CREATE TABLE documents_contracts PARTITION OF documents FOR VALUES IN ('contract');
CREATE TABLE documents_invoices PARTITION OF documents FOR VALUES IN ('invoice');
CREATE TABLE documents_receipts PARTITION OF documents FOR VALUES IN ('receipt');
CREATE TABLE documents_other PARTITION OF documents DEFAULT;
```

### Hash Partitioning (PostgreSQL 11+)

Distributes data uniformly when no natural range/list exists:

```sql
CREATE TABLE sessions (
    id UUID DEFAULT gen_random_uuid(),
    user_id BIGINT NOT NULL,
    data JSONB,
    PRIMARY KEY (id, user_id)
) PARTITION BY HASH (user_id);

CREATE TABLE sessions_p0 PARTITION OF sessions FOR VALUES WITH (MODULUS 4, REMAINDER 0);
CREATE TABLE sessions_p1 PARTITION OF sessions FOR VALUES WITH (MODULUS 4, REMAINDER 1);
CREATE TABLE sessions_p2 PARTITION OF sessions FOR VALUES WITH (MODULUS 4, REMAINDER 2);
CREATE TABLE sessions_p3 PARTITION OF sessions FOR VALUES WITH (MODULUS 4, REMAINDER 3);
```

### Key Rules

- **Partition pruning** is automatic when WHERE includes the partition key — verify with EXPLAIN
- Indexes on the parent propagate to all partitions automatically
- UNIQUE constraints must include the partition key
- Foreign keys pointing TO a partitioned table are supported in PostgreSQL 12+
- Aim for 10-100 partitions; thousands of partitions degrade planner performance

**Docs:** https://www.postgresql.org/docs/current/ddl-partitioning.html

---

## 2. Partition Management with pg_partman

pg_partman automates partition creation and retention. Essential for production.

```sql
-- Install extension
CREATE EXTENSION pg_partman SCHEMA partman;

-- Setup automatic monthly partitions
SELECT partman.create_parent(
    p_parent_table := 'public.events',
    p_control := 'event_time',
    p_type := 'native',
    p_interval := 'monthly',
    p_premake := 3  -- create 3 future partitions
);

-- Configure retention (auto-drop partitions older than 90 days)
UPDATE partman.part_config SET
    retention = '90 days',
    retention_keep_table = false,  -- true to detach but not drop
    infinite_time_partitions = true
WHERE parent_table = 'public.events';

-- Schedule maintenance with pg_cron
SELECT cron.schedule('partman-maintenance', '0 * * * *',
    $$CALL partman.run_maintenance_proc()$$
);
```

**Docs:** https://github.com/pgpartman/pg_partman

---

## 3. Django Integration

### Option A: RunSQL Migrations (simplest)

```python
class Migration(migrations.Migration):
    operations = [
        migrations.RunSQL(
            sql="""
            CREATE TABLE events (
                id BIGSERIAL,
                event_time TIMESTAMPTZ NOT NULL,
                tenant_id UUID NOT NULL,
                payload JSONB,
                PRIMARY KEY (id, event_time)
            ) PARTITION BY RANGE (event_time);
            
            CREATE TABLE events_2026_q1 PARTITION OF events
                FOR VALUES FROM ('2026-01-01') TO ('2026-04-01');
            """,
            reverse_sql="DROP TABLE IF EXISTS events CASCADE;",
        ),
    ]
```

Create a Django model with `managed = False`:
```python
class Event(models.Model):
    event_time = models.DateTimeField()
    tenant_id = models.UUIDField()
    payload = models.JSONField(default=dict)

    class Meta:
        managed = False
        db_table = "events"
```

### Option B: django-postgres-extra

Provides `PostgresPartitionedModel` for ORM-native partitioning:
```python
from psqlextra.types import PostgresPartitioningMethod
from psqlextra.models import PostgresPartitionedModel

class Event(PostgresPartitionedModel):
    class PartitioningMeta:
        method = PostgresPartitioningMethod.RANGE
        key = ["event_time"]

    event_time = models.DateTimeField()
    tenant_id = models.UUIDField()
    payload = models.JSONField(default=dict)
```

**Library:** https://github.com/SectorLabs/django-postgres-extra

The ORM works transparently — queries hit the parent table and PostgreSQL routes to the right partition.

---

## 4. Sharding with Citus

Citus extends PostgreSQL for horizontal scaling by distributing tables across nodes.

```sql
-- Install Citus extension
CREATE EXTENSION citus;

-- Distribute table by tenant_id
SELECT create_distributed_table('events', 'tenant_id');

-- Reference tables (small, replicated to all nodes)
SELECT create_reference_table('plans');
SELECT create_reference_table('countries');
```

### Citus + Django

Use `django-multitenant` which auto-appends `tenant_id` to all queries:
```python
from django_multitenant.models import TenantModel

class Event(TenantModel):
    tenant_id = 'tenant_id'  # distribution column
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    event_time = models.DateTimeField()
    payload = models.JSONField()
```

**Key rule:** All distributed tables that JOIN together must share the same distribution column (`tenant_id`). Co-located tenants ensure JOINs stay local to a single node.

Citus 12+ supports **schema-based sharding** where each schema is a logical shard.

**Docs:** https://docs.citusdata.com/en/stable/, https://github.com/citusdata/django-multitenant

---

## 5. Keyset Pagination

**Never use OFFSET for large tables.** OFFSET scans and discards rows — O(n) degradation.

### Instead: Keyset (cursor) pagination

```sql
-- First page
SELECT * FROM events WHERE tenant_id = $1
ORDER BY created_at DESC, id DESC LIMIT 20;

-- Next page (pass last row's created_at and id)
SELECT * FROM events WHERE tenant_id = $1
  AND (created_at, id) < ($last_created_at, $last_id)
ORDER BY created_at DESC, id DESC LIMIT 20;
```

In Django:
```python
# First page
events = Event.objects.filter(tenant=tenant).order_by("-created_at", "-id")[:20]

# Next page
last = events[len(events) - 1]
next_page = Event.objects.filter(
    tenant=tenant,
    created_at__lte=last.created_at,
).exclude(
    created_at=last.created_at, id__gte=last.id
).order_by("-created_at", "-id")[:20]
```

For API pagination, consider `django-rest-framework`'s `CursorPagination` which implements this pattern.

### Approximate Counts

For displaying total counts (pagination UI), avoid `COUNT(*)` on large tables:
```sql
-- Instant approximate count (updated by autovacuum)
SELECT reltuples::bigint FROM pg_class WHERE relname = 'events';
```

---

## 6. Archiving & Data Lifecycle

### Detach-Archive-Drop Pattern

The fastest way to remove old data from partitioned tables:

```sql
-- 1. Detach partition (instant, no data movement)
ALTER TABLE events DETACH PARTITION events_2025_q1;

-- 2. Archive to CSV / S3 (optional)
COPY events_2025_q1 TO '/tmp/events_2025_q1.csv' WITH CSV HEADER;
-- Upload to S3 with aws cli

-- 3. Drop the detached table
DROP TABLE events_2025_q1;
```

### Foreign Data Wrappers for Tiered Storage

Keep archived data queryable via `postgres_fdw`:

```sql
CREATE EXTENSION postgres_fdw;

CREATE SERVER archive_server FOREIGN DATA WRAPPER postgres_fdw
    OPTIONS (host 'archive-db.internal', dbname 'archive', port '5432');

CREATE USER MAPPING FOR django_user SERVER archive_server
    OPTIONS (user 'archive_reader', password 'xxx');

-- Create foreign table matching the partition structure
CREATE FOREIGN TABLE events_2025_q1
    PARTITION OF events
    FOR VALUES FROM ('2025-01-01') TO ('2025-04-01')
    SERVER archive_server;
```

This creates transparent tiered storage — queries spanning old and new data work seamlessly.

### COPY Protocol: Bulk Loading 10-100x Faster

PostgreSQL's COPY protocol is dramatically faster than INSERT for bulk data operations.
psycopg3 exposes it via `cursor.copy()`:

| Method | 10K rows | Speed |
|---|---|---|
| ORM `create()` one by one | ~13,000ms | 1x |
| `bulk_create(batch_size=500)` | ~460ms | ~28x |
| **COPY (psycopg3)** | **~190ms** | **~68x** |

```python
from django.db import connection
from psycopg import sql

def bulk_copy_employees(records):
    """COPY directly to Django model's table. 10-100x faster than bulk_create."""
    raw_conn = connection.connection  # underlying psycopg3 connection
    columns = ["tenant_id", "first_name", "paternal_surname", "curp",
               "hire_date", "position", "status", "daily_salary"]

    with raw_conn.cursor() as cur:
        copy_sql = sql.SQL("COPY employees ({}) FROM STDIN").format(
            sql.SQL(", ").join(sql.Identifier(c) for c in columns)
        )
        with cur.copy(copy_sql) as copy:
            for record in records:
                copy.write_row(record)
    raw_conn.commit()
```

**Staging + UPSERT pattern** for idempotent imports:
```python
with raw_conn.cursor() as cur:
    cur.execute("CREATE TEMP TABLE staging (LIKE employees INCLUDING ALL)")
    with cur.copy("COPY staging FROM STDIN") as copy:
        for row in data:
            copy.write_row(row)
    cur.execute("""
        INSERT INTO employees SELECT * FROM staging
        ON CONFLICT (tenant_id, curp)
        DO UPDATE SET daily_salary = EXCLUDED.daily_salary, updated_at = now()
    """)
    cur.execute("DROP TABLE staging")
raw_conn.commit()
```

**Caveats**: COPY bypasses the ORM — no `pre_save`/`post_save` signals, no model validation,
auto-increment sequences not updated. For multi-tenant, include `tenant_id` in the data.

### Batch Deletion for Non-Partitioned Tables

If partitioning isn't an option, delete in batches to avoid long locks:

```python
import time
from django.db import connection

def batch_delete(model, filter_kwargs, batch_size=5000, sleep_seconds=0.5):
    """Delete rows in batches to avoid long transactions and lock contention."""
    total = 0
    while True:
        with connection.cursor() as cursor:
            ids = list(
                model.objects.filter(**filter_kwargs)
                .values_list("id", flat=True)[:batch_size]
            )
            if not ids:
                break
            deleted, _ = model.objects.filter(id__in=ids).delete()
            total += deleted
        time.sleep(sleep_seconds)  # let autovacuum breathe
    return total
```

**Docs:** https://www.postgresql.org/docs/current/postgres-fdw.html
