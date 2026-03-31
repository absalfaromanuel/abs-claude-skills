# APIs & Multi-Service Architecture

## Table of Contents
1. PostgREST — Automatic REST APIs
2. GraphQL Options
3. LISTEN/NOTIFY — Event-Driven Patterns
4. Multi-Service Connection Patterns
5. Key Libraries and Resources

---

## 1. PostgREST — Automatic REST APIs

PostgREST converts a PostgreSQL schema into a full REST API. No code required — table structure defines the API.

### How It Works

- Each table/view in a schema becomes a REST endpoint
- Authentication via JWT mapped to PostgreSQL roles
- RLS policies control access
- Supports filtering, pagination, embedding (JOINs), and bulk operations
- Performance: ~2,000 req/s with <70MB memory

### Using Alongside Django

PostgREST handles simple CRUD while Django handles complex business logic:

```
Client → PostgREST → PostgreSQL (simple CRUD, real-time reads)
Client → Django → PostgreSQL (complex logic, workflows, integrations)
```

Both share the same database; RLS policies apply to both.

### Supabase Auto-API

Supabase includes PostgREST. Your tables automatically get REST endpoints:

```javascript
// Client-side (Supabase JS)
const { data } = await supabase
    .from('products')
    .select('*, category(name)')
    .eq('is_active', true)
    .order('created_at', { ascending: false })
    .limit(20)
```

From Django, you can also call Supabase's REST API for cross-service communication:

```python
import httpx

async def get_products():
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{SUPABASE_URL}/rest/v1/products",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {user_jwt}",
            },
            params={"is_active": "eq.true", "order": "created_at.desc"},
        )
        return response.json()
```

**Docs:** https://postgrest.org/, https://supabase.com/docs/guides/api

---

## 2. GraphQL Options

### pg_graphql (PostgreSQL extension)

Native PostgreSQL extension — no external server needed. Used by Supabase.

```sql
CREATE EXTENSION pg_graphql;

-- Query via SQL
SELECT graphql.resolve($$
    {
        productCollection(filter: {isActive: {eq: true}}, first: 10) {
            edges {
                node {
                    id
                    name
                    category { name }
                }
            }
        }
    }
$$);
```

### Hasura

Instant GraphQL API with subscriptions, permissions, and remote schemas:
- Auto-generates from PostgreSQL schema
- Real-time subscriptions
- Role-based access control
- Actions for custom business logic (can call Django endpoints)

### PostGraphile

Open-source, highly extensible GraphQL server for PostgreSQL:
- Plugin system for customization
- Supports RLS
- Excellent performance

### Django Graphene

If you want GraphQL within Django:
```python
# pip install graphene-django
import graphene
from graphene_django import DjangoObjectType

class ProductType(DjangoObjectType):
    class Meta:
        model = Product
        fields = ("id", "name", "price", "category")

class Query(graphene.ObjectType):
    products = graphene.List(ProductType)
    
    def resolve_products(root, info):
        return Product.objects.select_related("category").filter(is_active=True)
```

**Docs:** https://github.com/supabase/pg_graphql, https://hasura.io, https://www.graphile.org/postgraphile/

---

## 3. LISTEN/NOTIFY — Event-Driven Patterns

PostgreSQL includes built-in pub/sub. Notifications are **transactional** (delivered only on COMMIT).

### Basic Usage

```sql
-- Listener (in a persistent connection)
LISTEN data_changes;

-- Notifier (in application code or trigger)
NOTIFY data_changes, '{"table": "orders", "action": "INSERT", "id": 42}';
-- or
SELECT pg_notify('data_changes', '{"table": "orders", "action": "INSERT", "id": 42}');
```

Payload limit: **8000 bytes**. For larger data, send only the ID and have the listener fetch details.

### Trigger-Based Notifications

```sql
CREATE OR REPLACE FUNCTION notify_change() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('data_changes', json_build_object(
        'table', TG_TABLE_NAME,
        'action', TG_OP,
        'id', COALESCE(NEW.id, OLD.id),
        'tenant_id', COALESCE(NEW.tenant_id, OLD.tenant_id)
    )::text);
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER orders_notify
    AFTER INSERT OR UPDATE OR DELETE ON orders
    FOR EACH ROW EXECUTE FUNCTION notify_change();
```

### Python Listener with psycopg3

```python
import psycopg

def listen_for_changes():
    # IMPORTANT: Use a dedicated connection, not from the pool, with autocommit=True
    conn = psycopg.connect(DSN, autocommit=True)
    conn.execute("LISTEN data_changes")
    
    for notify in conn.notifies(timeout=30):
        payload = json.loads(notify.payload)
        process_change(payload)
```

### Async Listener (for FastAPI / Django async views)

```python
import psycopg

async def listen_events_async():
    aconn = await psycopg.AsyncConnection.connect(DSN, autocommit=True)
    await aconn.execute("LISTEN data_changes")
    async for notify in aconn.notifies():
        await process_event(json.loads(notify.payload))
```

### Bridge Pattern: LISTEN/NOTIFY → Celery

For reliable processing, bridge PostgreSQL events to a proper message queue:

```python
import json
import psycopg
from celery import Celery

app = Celery("myapp")

def pg_listener():
    """Long-running process that bridges PG events to Celery tasks."""
    conn = psycopg.connect(DSN, autocommit=True)
    conn.execute("LISTEN data_changes")
    
    for notify in conn.notifies():
        payload = json.loads(notify.payload)
        
        # Route to appropriate Celery task
        if payload["table"] == "orders" and payload["action"] == "INSERT":
            app.send_task("tasks.process_new_order", args=[payload["id"]])
        elif payload["table"] == "orders" and payload["action"] == "UPDATE":
            app.send_task("tasks.sync_order_status", args=[payload["id"]])
```

### Limitations

- **Not a message queue**: no persistence, no delivery guarantee for disconnected listeners
- **Doesn't work through PgBouncer** in transaction mode — requires a dedicated, non-pooled connection
- **Single database scope**: notifications don't cross database boundaries
- Listeners must maintain a persistent connection

Use cases: cache invalidation, real-time UI updates, triggering background jobs. For reliable messaging, use RabbitMQ/Redis Streams with this as a bridge.

**Docs:** https://www.postgresql.org/docs/current/sql-notify.html, https://www.postgresql.org/docs/current/sql-listen.html

---

## 4. Multi-Service Connection Patterns

### Connection Pooling for Multiple Services

When multiple services connect to the same PostgreSQL:

```
Service A (Django) ──→ PgBouncer ──→ PostgreSQL
Service B (API)    ──→ PgBouncer ──→ PostgreSQL  
Service C (Worker) ──→ PgBouncer ──→ PostgreSQL
```

Configure PgBouncer with per-service pools:
```ini
[databases]
mydb_web = host=pg.internal dbname=mydb pool_size=30
mydb_api = host=pg.internal dbname=mydb pool_size=20
mydb_worker = host=pg.internal dbname=mydb pool_size=10
```

### Read/Write Splitting

```python
# Django database router for read replicas
class ReadReplicaRouter:
    REPLICA_MODELS = {"analytics", "reports"}  # apps that use replica
    
    def db_for_read(self, model, **hints):
        if model._meta.app_label in self.REPLICA_MODELS:
            return "replica"
        # Check if explicitly requested
        if hints.get("instance") and hasattr(hints["instance"], "_use_replica"):
            return "replica"
        return "default"
    
    def db_for_write(self, model, **hints):
        return "default"
```

### Service-to-Service via Database

For microservices sharing a database, use PostgreSQL schemas for logical separation:

```sql
CREATE SCHEMA billing;
CREATE SCHEMA inventory;

-- Service A only accesses billing schema
GRANT USAGE ON SCHEMA billing TO billing_service;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA billing TO billing_service;

-- Service B only accesses inventory schema
GRANT USAGE ON SCHEMA inventory TO inventory_service;
```

### Outbox Pattern for Reliable Events

When you need guaranteed event delivery across services:

```python
class OutboxEvent(models.Model):
    id = models.BigAutoField(primary_key=True)
    event_type = models.CharField(max_length=100)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True)
    
    class Meta:
        indexes = [
            models.Index(
                fields=["created_at"],
                condition=models.Q(published_at__isnull=True),
                name="idx_outbox_unpublished",
            ),
        ]

# In your business logic — same transaction as the main operation
with transaction.atomic():
    order = Order.objects.create(customer=customer, total=total)
    OutboxEvent.objects.create(
        event_type="order.created",
        payload={"order_id": order.id, "total": str(total)},
    )

# Separate worker polls and publishes
def publish_outbox_events():
    events = OutboxEvent.objects.filter(published_at__isnull=True).order_by("id")[:100]
    for event in events:
        publish_to_queue(event.event_type, event.payload)
        event.published_at = timezone.now()
        event.save(update_fields=["published_at"])
```

---

## 5. Key Libraries and Resources

### Django Libraries

| Library | Purpose | URL |
|---------|---------|-----|
| django-multitenant | Multi-tenant with Citus | https://github.com/citusdata/django-multitenant |
| django-tenants | Schema-based multi-tenancy | https://github.com/django-tenants/django-tenants |
| django-pghistory | Audit trails via triggers | https://github.com/AmbitionEng/django-pghistory |
| django-pgtrigger | PostgreSQL triggers from Django | https://github.com/AmbitionEng/django-pgtrigger |
| django-postgres-extra | Upserts, partitioning | https://github.com/SectorLabs/django-postgres-extra |
| django-pg-zero-downtime-migrations | Safe production migrations | https://github.com/tbicr/django-pg-zero-downtime-migrations |
| django-redis | Advanced Redis caching | https://github.com/jazzband/django-redis |
| django-ltree | Hierarchical data | https://github.com/mariocesar/django-ltree |

### PostgreSQL Extensions

| Extension | Purpose | Docs |
|-----------|---------|------|
| pg_stat_statements | Query statistics | https://www.postgresql.org/docs/current/pgstatstatements.html |
| pgAudit | Compliance auditing | https://github.com/pgaudit/pgaudit |
| pgcrypto | Encryption functions | https://www.postgresql.org/docs/current/pgcrypto.html |
| pg_partman | Partition management | https://github.com/pgpartman/pg_partman |
| pg_cron | Job scheduling | https://github.com/citusdata/pg_cron |
| pg_trgm | Trigram fuzzy search | https://www.postgresql.org/docs/current/pgtrgm.html |
| postgis | Geospatial data | https://postgis.net/ |
| Citus | Horizontal sharding | https://github.com/citusdata/citus |

### Official Documentation

- PostgreSQL: https://www.postgresql.org/docs/current/index.html
- Django databases: https://docs.djangoproject.com/en/5.2/ref/databases/
- Django contrib.postgres: https://docs.djangoproject.com/en/5.2/ref/contrib/postgres/
- psycopg3: https://www.psycopg.org/psycopg3/docs/
- PostgreSQL Wiki: https://wiki.postgresql.org

### Communities

- Stack Overflow: https://stackoverflow.com/questions/tagged/postgresql
- Reddit: https://www.reddit.com/r/PostgreSQL/ and https://www.reddit.com/r/django/
- Planet PostgreSQL (blog aggregator): https://planet.postgresql.org/
- Mailing lists: https://lists.postgresql.org/
- Django Forum: https://forum.djangoproject.com/

### Tools

- pgTune (config generator): https://pgtune.leopard.in.ua/
- pgcli (enhanced psql): https://www.pgcli.com/
- pgAdmin 4 (GUI): https://www.pgadmin.org/
- DBeaver (multi-DB GUI): https://dbeaver.io/
- pgBadger (log analysis): https://github.com/darold/pgbadger
- explain.depesz.com (EXPLAIN visualizer): https://explain.depesz.com/
