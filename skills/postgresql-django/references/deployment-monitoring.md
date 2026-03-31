# Deployment & Monitoring

## Table of Contents
1. AWS RDS / Aurora Configuration
2. Supabase
3. Backups & Disaster Recovery
4. Monitoring Metrics & Tools
5. Caching with Redis

---

## 1. AWS RDS / Aurora Configuration

### Instance Selection

- **db.r7g** (Graviton3, memory-optimized): best price/performance for production
- **db.t4g** (burstable): development/staging only
- Use **Multi-AZ DB Cluster** (1 writer + 2 readable standbys, ~35s failover)

### Recommended Parameters

For a db.r6g.xlarge (4 vCPU, 32GB RAM):

```
# Memory
shared_buffers = {DBInstanceClassMemory/4}        # ~8 GB
effective_cache_size = {DBInstanceClassMemory*3/4}  # ~24 GB
work_mem = 65536                                    # 64 MB
maintenance_work_mem = 524288                       # 512 MB

# I/O (SSDs)
random_page_cost = 1.1
effective_io_concurrency = 200
seq_page_cost = 1.0

# WAL
wal_buffers = 65536         # 64 MB
checkpoint_completion_target = 0.9
max_wal_size = 2048         # 2 GB

# Autovacuum
autovacuum_vacuum_scale_factor = 0.05
autovacuum_analyze_scale_factor = 0.02
autovacuum_vacuum_cost_limit = 1000
autovacuum_max_workers = 4

# Monitoring
shared_preload_libraries = 'pg_stat_statements'
pg_stat_statements.track = 'all'
log_min_duration_statement = 1000      # log queries > 1s
log_checkpoints = on

# Safety
idle_in_transaction_session_timeout = 30000  # 30s
statement_timeout = 30000                     # 30s
```

### RDS Proxy

For Lambda / serverless or high connection churn ($0.015/vCPU-hour):
- Maintains idle connections during failovers
- Pin avoidance: avoid SET statements, temp tables, prepared statements
- Not needed if using Django 5.1+ native pooling

**Note:** For detailed connection pooling configuration (Django native pool, PgBouncer, prepared
statements, server-side cursors) see `references/django-orm.md` Section 5.

### Aurora PostgreSQL

Choose over standard RDS when you need:
- Storage distributed across 6 copies in 3 AZs
- Up to 15 read replicas with <100ms lag
- Faster failover (5-30s vs 35-120s)
- Global Database for multi-region (<1s lag)
- ~20% cost premium over standard RDS

### Django with Read Replicas

```python
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": os.environ["DB_WRITER_HOST"],
        # ... other settings
    },
    "replica": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": os.environ["DB_READER_HOST"],
        # ... other settings
    },
}

# Router
class PrimaryReplicaRouter:
    def db_for_read(self, model, **hints):
        return "replica"

    def db_for_write(self, model, **hints):
        return "default"

    def allow_relation(self, obj1, obj2, **hints):
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return db == "default"

DATABASE_ROUTERS = ["myapp.routers.PrimaryReplicaRouter"]
```

**Docs:** https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_PostgreSQL.html

---

## 2. Supabase

### Architecture

Supabase provides a dedicated PostgreSQL instance with integrated services:
- **PostgREST**: auto-generated REST API from your schema
- **pg_graphql**: GraphQL endpoint
- **Supabase Auth**: JWT-based, maps to PostgreSQL roles
- **Realtime**: WebSocket subscriptions via PostgreSQL replication
- **Storage**: S3-compatible with RLS policies
- **Edge Functions**: Deno-based serverless functions

### Connecting Django to Supabase

```python
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "postgres",
        "USER": "postgres.your-project-ref",
        "PASSWORD": os.environ["SUPABASE_DB_PASSWORD"],
        "HOST": "aws-0-us-east-1.pooler.supabase.com",
        "PORT": "6543",  # Pooled connection (Supavisor)
        "OPTIONS": {
            "sslmode": "require",
        },
    }
}
```

Use port 6543 (transaction mode pooling via Supavisor) for Django.
Use port 5432 (direct connection) only for migrations.

### RLS with Supabase

Supabase heavily leverages PostgreSQL RLS. Policies use `auth.uid()` and `auth.jwt()`:

```sql
-- Supabase-style RLS
CREATE POLICY "Users see own data" ON profiles
    FOR SELECT USING (auth.uid() = user_id);
```

When using Django alongside Supabase, you can use standard PostgreSQL RLS with `current_setting()` as described in the security reference.

### Cost Comparison

Supabase Pro (~$25/month base + compute) is significantly cheaper than equivalent AWS infrastructure for early-stage SaaS. Lock-in risk is low since RLS policies are standard SQL.

**Docs:** https://supabase.com/docs/guides/database/overview

---

## 3. Backups & Disaster Recovery

### RDS Automated Backups

- Daily snapshots + continuous WAL archiving
- Point-in-Time Recovery (PITR) to any second within retention (1-35 days)
- Cross-region: copy snapshots manually or use cross-region read replicas

### pgBackRest (self-managed)

Enterprise-grade backup tool:
- Incremental backups (only changed blocks)
- Parallel backup/restore
- Archive to S3/GCS
- Encryption and compression
- PITR support

### Backup Strategy

Define RPO (Recovery Point Objective) and RTO (Recovery Time Objective):

| Strategy | RPO | RTO |
|----------|-----|-----|
| Multi-AZ (RDS) | ~0 | 35s - 2min |
| Streaming replication | seconds | minutes |
| Automated backups + WAL | ~5 min | minutes - hours |
| Daily snapshots only | 24 hours | hours |

**Critical: Test restores regularly.** Schedule quarterly restore drills.

### Django-Level Backups

For application-level data exports:
```bash
python manage.py dumpdata --natural-primary --natural-foreign -o backup.json
python manage.py loaddata backup.json
```

Use `pg_dump` for proper database-level backups:
```bash
pg_dump -Fc -h host -U user dbname > backup.dump
pg_restore -d dbname backup.dump
```

**Docs:** https://www.postgresql.org/docs/current/continuous-archiving.html

---

## 4. Monitoring Metrics & Tools

### Essential Metrics

```sql
-- 1. Cache hit ratio (target: > 99%)
SELECT round(
    sum(heap_blks_hit) / NULLIF(sum(heap_blks_hit + heap_blks_read), 0)::numeric * 100, 2
) AS cache_hit_ratio
FROM pg_statio_user_tables;

-- 2. Connection usage
SELECT count(*) AS total,
       count(*) FILTER (WHERE state = 'active') AS active,
       count(*) FILTER (WHERE state = 'idle') AS idle,
       count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn
FROM pg_stat_activity WHERE datname = current_database();

-- 3. Top queries by total time (requires pg_stat_statements)
SELECT query, calls,
       round(total_exec_time::numeric, 2) AS total_ms,
       round(mean_exec_time::numeric, 2) AS mean_ms,
       rows
FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10;

-- 4. Table sizes
SELECT relname,
       pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
       pg_size_pretty(pg_relation_size(relid)) AS table_size,
       pg_size_pretty(pg_total_relation_size(relid) - pg_relation_size(relid)) AS index_size
FROM pg_catalog.pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 20;

-- 5. Dead tuples (vacuum needed?)
SELECT relname, n_dead_tup, n_live_tup,
       round(n_dead_tup::numeric / NULLIF(n_live_tup, 0) * 100, 1) AS dead_pct,
       last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables WHERE n_dead_tup > 1000 ORDER BY n_dead_tup DESC;

-- 6. Index usage
SELECT indexrelname, idx_scan, idx_tup_read,
       pg_size_pretty(pg_relation_size(indexrelid)) AS size
FROM pg_stat_user_indexes ORDER BY idx_scan ASC LIMIT 20;

-- 7. Replication lag (if using replicas)
SELECT client_addr,
       pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS lag_bytes,
       replay_lag
FROM pg_stat_replication;

-- 8. Lock contention
SELECT blocked_locks.pid AS blocked_pid,
       blocking_locks.pid AS blocking_pid,
       blocked_activity.query AS blocked_query
FROM pg_catalog.pg_locks blocked_locks
JOIN pg_catalog.pg_locks blocking_locks
    ON blocking_locks.locktype = blocked_locks.locktype
    AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
    AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
    AND blocking_locks.pid != blocked_locks.pid
JOIN pg_catalog.pg_stat_activity blocked_activity ON blocked_activity.pid = blocked_locks.pid
WHERE NOT blocked_locks.granted;
```

### Monitoring Tools

| Tool | Type | Best For |
|------|------|----------|
| **pganalyze** | SaaS | Deep query analysis, index advisor, VACUUM advisor |
| **Datadog** | SaaS | 100+ PostgreSQL metrics, APM integration |
| **Grafana + postgres_exporter** | Open-source | Custom dashboards, alerts |
| **pgBadger** | Open-source | Log analysis, HTML reports |
| **pg_stat_monitor** | Extension | Enhanced pg_stat_statements |
| **AWS CloudWatch** | AWS native | RDS/Aurora metrics, alarms |

### Alerting Thresholds

Set alerts for:
- Cache hit ratio < 98%
- Connection count > 80% of max_connections
- Replication lag > 10s
- Dead tuple ratio > 10%
- Query time p99 > 1s
- Disk usage > 80%
- CPU usage sustained > 80%

**Docs:** https://www.postgresql.org/docs/current/monitoring-stats.html

---

## 5. Caching with Redis

### Django Configuration

```python
# Django 4.0+ built-in Redis cache
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.environ.get("REDIS_URL", "redis://127.0.0.1:6379"),
        "KEY_PREFIX": "myapp",
        "TIMEOUT": 300,  # 5 minutes default TTL
    }
}

# For advanced features (compression, connection pool, Sentinel)
# pip install django-redis
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": "redis://127.0.0.1:6379/0",
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "COMPRESSOR": "django_redis.compressors.zlib.ZlibCompressor",
            "CONNECTION_POOL_KWARGS": {"max_connections": 50},
        },
    }
}
```

### Caching Patterns

```python
from django.core.cache import cache
from django.views.decorators.cache import cache_page

# Cache-aside pattern
def get_product(product_id):
    cache_key = f"product:{product_id}"
    product = cache.get(cache_key)
    if product is None:
        product = Product.objects.select_related("category").get(id=product_id)
        cache.set(cache_key, product, timeout=600)
    return product

# Invalidation via signals
from django.db.models.signals import post_save, post_delete

@receiver([post_save, post_delete], sender=Product)
def invalidate_product_cache(sender, instance, **kwargs):
    cache.delete(f"product:{instance.id}")

# View-level caching
@cache_page(60 * 15)  # 15 minutes
def product_list(request):
    ...

# Template fragment caching
# {% cache 300 product_card product.id %}...{% endcache %}

# QuerySet caching with django-cacheops (optional)
# pip install django-cacheops
```

### Cache + Materialized View Strategy

For analytics dashboards:
1. Materialized view refreshes every 5 minutes (PostgreSQL-side cache)
2. Redis caches the Django serialized response (application-side cache)
3. CDN caches the API response (network-side cache)

Target: **>80% cache hit ratio** measured via Redis INFO stats.

**Library:** https://github.com/jazzband/django-redis
