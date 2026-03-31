---
name: postgresql-django
description: >
  Best practices for PostgreSQL + Django in production SaaS. Use this skill for database
  design, Django ORM optimization, schema creation, query performance, indexing, partitioning,
  Row Level Security, multi-tenant architecture, migrations, connection pooling, monitoring,
  or deploying on AWS RDS/Aurora/Supabase. Also trigger for Django REST Framework viewsets,
  serializers, CursorPagination, API filtering/throttling, Celery tasks, Django 6.0 background
  tasks, transaction.on_commit, periodic jobs. Trigger on: models.py, EXPLAIN ANALYZE, VACUUM,
  pg_stat_statements, N+1 queries, select_related, prefetch_related. Even for simple requests
  like "create a model", "design the database", "build an API", or "add a background task"
  in Django — use this skill to ensure best practices.
---

# PostgreSQL + Django: Production Best Practices

This skill provides battle-tested patterns for building scalable Django applications on PostgreSQL.
It covers the full lifecycle: schema design → optimization → security → deployment → monitoring.

## How to Use This Skill

This skill is organized into reference files by domain. Read the relevant reference(s) based
on what the user needs. You don't need to read all of them — pick the ones that apply.

### Reference Files

| File | When to Read |
|------|-------------|
| `references/schema-design.md` | Creating models, designing tables, normalization, data types, naming conventions, multi-tenant architecture |
| `references/indexing-optimization.md` | Slow queries, EXPLAIN ANALYZE, choosing indexes, VACUUM, autovacuum, query planner statistics |
| `references/partitioning-sharding.md` | Tables with millions+ rows, time-series data, archiving old data, horizontal scaling with Citus |
| `references/security.md` | Row Level Security, roles/permissions, encryption, auditing, hardening checklist, multi-tenant isolation |
| `references/django-orm.md` | Django models, ORM optimization, N+1 queries, migrations, connection pooling, PostgreSQL-specific fields, useful extensions |
| `references/deployment-monitoring.md` | AWS RDS/Aurora config, Supabase setup, backups, monitoring metrics, pg_stat_statements, caching with Redis |
| `references/apis-architecture.md` | PostgREST, LISTEN/NOTIFY, event-driven patterns, connecting multiple services |
| `references/drf-api.md` | Django REST Framework viewsets, serializer optimization, pagination (CursorPagination for large tables), filtering, caching API responses, throttling, JWT auth for SaaS |
| `references/background-tasks.md` | Django 6.0 Tasks framework, Celery setup, transaction.on_commit, tenant-aware tasks, outbox pattern, periodic tasks, retry strategies, monitoring with Flower |

### Core Principles (Always Apply)

These principles apply to every PostgreSQL + Django task regardless of domain:

1. **Normalize to 3NF, desnormalize only when EXPLAIN ANALYZE proves a bottleneck.** Don't optimize prematurely — measure first.

2. **Always use `select_related()` / `prefetch_related()`.** The N+1 problem is the #1 cause of slow Django apps. Every queryset that touches related models must use one of these.

3. **Choose the right index type for each access pattern:**
   - B-tree (default): equality, range, ORDER BY — covers 90% of cases
   - GIN: JSONB, arrays, full-text search
   - BRIN: time-series / append-only tables with millions+ rows (100-1000x smaller than B-tree)
   - GiST: geometric data, range types, nearest-neighbor

4. **Use `timestamptz` (never `timestamp` without timezone)** for all datetime columns.

5. **Use `BIGINT GENERATED ALWAYS AS IDENTITY`** for internal PKs, `UUID` for external/API-facing IDs.

6. **Set `random_page_cost = 1.1`** on SSDs (default 4.0 is for spinning disks).

7. **Enable `pg_stat_statements`** in production from day one (only ~2-3% CPU overhead).

8. **Use `snake_case` lowercase** for all database identifiers — PostgreSQL folds unquoted identifiers to lowercase.

9. **Never disable autovacuum.** Tune it: `scale_factor = 0.05`, `cost_limit = 1000`.

10. **Test migrations with `sqlmigrate` before production.** Use `atomic = False` for `CREATE INDEX CONCURRENTLY`.

### Django Settings Template

When configuring a new Django + PostgreSQL project, recommend this baseline:

```python
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ["DB_NAME"],
        "USER": os.environ["DB_USER"],
        "PASSWORD": os.environ["DB_PASSWORD"],
        "HOST": os.environ["DB_HOST"],
        "PORT": os.environ.get("DB_PORT", "5432"),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "pool": {  # Django 5.1+ with psycopg3
                "min_size": 4,
                "max_size": 16,
                "timeout": 10,
                "max_lifetime": 1800,
            },
            "options": "-c default_transaction_isolation=read\\ committed -c timezone=UTC",
        },
    }
}
```

For Django < 5.1 or psycopg2, use `CONN_MAX_AGE = 600` instead of the pool option,
and add PgBouncer externally for connection pooling.

### Model Template

When creating Django models for PostgreSQL, follow this pattern:

```python
import uuid
from django.db import models
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex, BrinIndex

class BaseModel(models.Model):
    """Abstract base for all models — consistent timestamps and UUID."""
    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TenantModel(BaseModel):
    """Abstract base for multi-tenant models — adds tenant FK and index."""
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="%(class)s_set",
    )

    class Meta:
        abstract = True
        # Every query filters by tenant — this index is essential
        indexes = [
            models.Index(fields=["tenant", "created_at"], name="idx_%(class)s_tenant_created"),
        ]
```

### Quick Decision Trees

**"How should I handle multi-tenancy?"**
- < 500 tenants with strong isolation needs → Schema-based (`django-tenants`)
- Any scale, shared tables OK → Shared schema + `tenant_id` + RLS (`django-multitenant`)
- Massive scale + horizontal scaling → Citus with `django-multitenant`

**"My query is slow, what do I do?"**
1. Run `qs.explain(analyze=True, buffers=True)` in Django
2. Look for Seq Scans on large tables → add index
3. Check rows estimated vs actual → run `ANALYZE` on the table
4. Check for N+1 → add `select_related` / `prefetch_related`
5. Read `references/indexing-optimization.md` for deeper diagnosis

**"How should I handle a table with millions of rows?"**
1. Add BRIN index on the timestamp column
2. Consider declarative partitioning by date range
3. Use keyset pagination (not OFFSET)
4. Archive old partitions with Detach-Archive-Drop
5. Read `references/partitioning-sharding.md` for full strategy

**"How do I deploy PostgreSQL for production?"**
- Small/medium SaaS → Supabase (built-in RLS, Auth, REST API, lower cost)
- Enterprise / compliance-heavy → AWS RDS/Aurora PostgreSQL
- Read `references/deployment-monitoring.md` for configuration details

**"How should I paginate my DRF API?"**
- < 10K rows → `PageNumberPagination` (simple page numbers)
- 10K-1M rows → `LimitOffsetPagination` (watch OFFSET degradation)
- > 1M rows → `CursorPagination` (O(1), no COUNT(*), requires indexed ordering field)
- Read `references/drf-api.md` for serializer optimization and caching strategies

**"Should I use Celery or Django 6.0 Tasks?"**
- Simple fire-and-forget (emails, PDFs) → Django 6.0 `@task` with `django-tasks` DatabaseBackend
- Need scheduling, retries, chaining → Celery with Redis broker
- Both can coexist — use Django Tasks for simple jobs, Celery for complex workflows
- **Always use `transaction.on_commit()`** when dispatching tasks after DB writes
- Read `references/background-tasks.md` for full patterns

### Key Documentation Links

- PostgreSQL Official Docs: https://www.postgresql.org/docs/current/index.html
- Django 6.0 Docs: https://docs.djangoproject.com/en/6.0/
- Django Database Optimization: https://docs.djangoproject.com/en/6.0/topics/db/optimization/
- Django PostgreSQL Features: https://docs.djangoproject.com/en/6.0/ref/contrib/postgres/
- Django Tasks Framework: https://docs.djangoproject.com/en/6.0/topics/tasks/
- Django REST Framework: https://www.django-rest-framework.org/
- psycopg3 Docs: https://www.psycopg.org/psycopg3/docs/
- Celery + Django: https://docs.celeryq.dev/en/stable/django/first-steps-with-django.html
- PostgreSQL Wiki: https://wiki.postgresql.org
- pgTune (config generator): https://pgtune.leopard.in.ua/
