# Background Tasks + PostgreSQL

## Table of Contents
1. Django 6.0 Tasks Framework (built-in)
2. Celery with PostgreSQL
3. Database Transactions and Tasks
4. Task Patterns for SaaS
5. PostgreSQL as Event Source (LISTEN/NOTIFY bridge)
6. Monitoring and Reliability

---

## 1. Django 6.0 Tasks Framework (built-in)

Django 6.0 introduces `django.tasks` — a native API for defining and enqueuing background tasks. It provides the contract (task definition, queuing, result tracking) while external backends handle execution.

### Defining Tasks

```python
# myapp/tasks.py
from django.tasks import task
from django.core.mail import send_mail

@task
def send_welcome_email(user_email, username):
    """Simple background task."""
    send_mail(
        subject=f"Bienvenido, {username}!",
        message="Gracias por registrarte en Partner Laboral.",
        from_email=None,
        recipient_list=[user_email],
    )
    return f"Email sent to {user_email}"

@task(priority=10, queue_name="documents")
def generate_settlement_pdf(settlement_id):
    """Higher priority task on a dedicated queue."""
    from myapp.models import Settlement
    settlement = Settlement.objects.select_related("employee", "tenant").get(id=settlement_id)
    # ... generate PDF with WeasyPrint
    return f"PDF generated for settlement {settlement_id}"
```

### Enqueuing Tasks

```python
# In a view or service
from myapp.tasks import send_welcome_email, generate_settlement_pdf

# Enqueue for background execution
result = send_welcome_email.enqueue(user_email="user@example.com", username="Carlos")

# Check status
print(result.status)    # READY → RUNNING → SUCCESSFUL / FAILED
print(result.result)    # "Email sent to user@example.com"
```

### Configuration for Production

Django 6.0 only ships development/testing backends. For production, use `django-tasks` with its `DatabaseBackend`:

```python
# pip install django-tasks
# settings.py
INSTALLED_APPS = [
    "django_tasks",
    "django_tasks.backends.database",
    # ...
]

TASKS = {
    "default": {
        "BACKEND": "django_tasks.backends.database.DatabaseBackend",
    },
    "documents": {  # Dedicated queue for document generation
        "BACKEND": "django_tasks.backends.database.DatabaseBackend",
        "OPTIONS": {"queue_name": "documents"},
    },
}
```

Run the worker:
```bash
python manage.py migrate        # creates task tables
python manage.py db_worker      # starts processing tasks
```

### When Django Tasks vs Celery

| Feature | Django 6.0 Tasks | Celery |
|---------|-----------------|--------|
| Setup complexity | Minimal | Requires broker (Redis/RabbitMQ) |
| Task scheduling (cron) | Not supported | `celery beat` |
| Retries | Backend-dependent | Built-in with backoff |
| Task chaining/groups | Not supported | Chords, groups, chains |
| Monitoring | Basic | Flower, events |
| Maturity | New (Django 6.0) | Battle-tested (10+ years) |

**Recommendation:** Use Django Tasks for simple fire-and-forget jobs (emails, PDF generation, notifications). Use Celery when you need scheduling, retries with exponential backoff, task chains, or heavy distributed processing.

**Docs:** https://docs.djangoproject.com/en/6.0/topics/tasks/, https://docs.djangoproject.com/en/6.0/ref/tasks/

---

## 2. Celery with PostgreSQL

### Setup

```python
# proj/celery.py
import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "proj.settings")

app = Celery("proj")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
```

```python
# settings.py
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = "django-db"   # Store results in PostgreSQL
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"

# Connection management
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# Task reliability
CELERY_TASK_ACKS_LATE = True          # Acknowledge after execution, not before
CELERY_WORKER_PREFETCH_MULTIPLIER = 1  # Prevent worker from grabbing too many tasks
CELERY_TASK_REJECT_ON_WORKER_LOST = True

# Result expiration (clean up old results)
CELERY_RESULT_EXPIRES = 86400  # 24 hours
```

### Concurrency Model

Choose based on workload:
- **prefork** (default): CPU-bound tasks (PDF generation, calculations)
- **gevent/eventlet**: I/O-bound tasks (API calls, email sending)

```bash
# CPU-bound
celery -A proj worker --concurrency=4 --pool=prefork

# I/O-bound
celery -A proj worker --concurrency=100 --pool=gevent
```

### Periodic Tasks with celery-beat

```python
# settings.py
from celery.schedules import crontab

CELERY_BEAT_SCHEDULE = {
    "refresh-materialized-views": {
        "task": "myapp.tasks.refresh_materialized_views",
        "schedule": crontab(minute="*/5"),  # every 5 minutes
    },
    "cleanup-expired-sessions": {
        "task": "myapp.tasks.cleanup_sessions",
        "schedule": crontab(hour=3, minute=0),  # daily at 3am
    },
    "partition-maintenance": {
        "task": "myapp.tasks.run_partman_maintenance",
        "schedule": crontab(minute=0),  # every hour
    },
}
```

**Docs:** https://docs.celeryq.dev/en/stable/django/first-steps-with-django.html

---

## 3. Database Transactions and Tasks

The #1 bug with Celery + Django: **the task starts before the transaction commits**, so it can't find the data it needs.

### The Problem

```python
# BAD: Race condition — task may execute before transaction commits
from django.db import transaction

def create_settlement(request):
    with transaction.atomic():
        settlement = Settlement.objects.create(...)
        generate_settlement_pdf.delay(settlement.id)  # May fail with DoesNotExist!
    return JsonResponse({"id": settlement.id})
```

### Solution 1: transaction.on_commit (recommended)

```python
from django.db import transaction

def create_settlement(request):
    with transaction.atomic():
        settlement = Settlement.objects.create(...)
        # Task only dispatched AFTER transaction commits successfully
        transaction.on_commit(
            lambda: generate_settlement_pdf.delay(settlement.id)
        )
    return JsonResponse({"id": settlement.id})
```

### Solution 2: delay_on_commit (Celery 5.4+)

```python
# Celery 5.4+ provides a shortcut
def create_settlement(request):
    with transaction.atomic():
        settlement = Settlement.objects.create(...)
        generate_settlement_pdf.delay_on_commit(settlement.id)
    return JsonResponse({"id": settlement.id})
```

Note: `delay_on_commit` does NOT return a task ID (the task hasn't been sent yet).

### Solution 3: Django 6.0 Tasks (inherently safe)

Django 6.0's `enqueue()` already handles this properly when using the DatabaseBackend — the task is written to the same database in the same transaction:

```python
from django.db import transaction
from myapp.tasks import generate_settlement_pdf

def create_settlement(request):
    with transaction.atomic():
        settlement = Settlement.objects.create(...)
        generate_settlement_pdf.enqueue(settlement.id)  # Same transaction!
```

### Testing Considerations

`Django's TestCase` wraps each test in a transaction that is rolled back — `on_commit` callbacks never execute. Use `TransactionTestCase` or override with `@override_settings(CELERY_TASK_ALWAYS_EAGER=True)`.

**Docs:** https://docs.djangoproject.com/en/6.0/topics/db/transactions/#performing-actions-after-commit

---

## 4. Task Patterns for SaaS

### Tenant-Aware Tasks

Always pass `tenant_id` explicitly — don't rely on thread-local state:

```python
# Celery
@shared_task(bind=True, max_retries=3)
def generate_report(self, tenant_id, report_type, date_range):
    """Tenant-aware task with retry logic."""
    try:
        tenant = Tenant.objects.get(id=tenant_id)
        # Set RLS context for any raw SQL
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])

        # Business logic here
        report = build_report(tenant, report_type, date_range)
        return {"status": "success", "url": report.url}

    except Tenant.DoesNotExist:
        return {"status": "error", "message": "Tenant not found"}
    except Exception as exc:
        self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))  # exponential backoff


# Django 6.0 Tasks
@task
def generate_report(tenant_id, report_type, date_range):
    tenant = Tenant.objects.get(id=tenant_id)
    report = build_report(tenant, report_type, date_range)
    return {"status": "success", "url": report.url}
```

### Outbox Pattern for Reliable Events

When you need guaranteed event delivery (webhooks, cross-service notifications):

```python
class OutboxEvent(models.Model):
    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    event_type = models.CharField(max_length=100)  # "settlement.created"
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

# Write event in same transaction as business logic
with transaction.atomic():
    settlement = Settlement.objects.create(...)
    OutboxEvent.objects.create(
        tenant=settlement.tenant,
        event_type="settlement.created",
        payload={"settlement_id": settlement.id, "type": settlement.settlement_type},
    )

# Periodic task polls and publishes
@shared_task
def publish_outbox_events():
    events = OutboxEvent.objects.filter(published_at__isnull=True).order_by("id")[:100]
    for event in events:
        publish_to_webhook(event)
        event.published_at = timezone.now()
        event.save(update_fields=["published_at"])
```

### Bulk Processing with Batched Tasks

For operations on large datasets, process in batches to avoid memory issues and long-running transactions:

```python
@shared_task
def recalculate_all_settlements(tenant_id, batch_size=500):
    """Process settlements in batches using keyset pagination."""
    last_id = 0
    total_processed = 0

    while True:
        ids = list(
            Settlement.objects.filter(
                tenant_id=tenant_id, status="draft", id__gt=last_id
            ).order_by("id").values_list("id", flat=True)[:batch_size]
        )
        if not ids:
            break

        for settlement_id in ids:
            recalculate_single_settlement.delay(settlement_id)

        last_id = ids[-1]
        total_processed += len(ids)

    return {"total_dispatched": total_processed}
```

---

## 5. PostgreSQL as Event Source (LISTEN/NOTIFY Bridge)

Bridge PostgreSQL events to your task system for real-time reactions:

```python
# management/commands/pg_event_bridge.py
import json
import psycopg
from django.core.management.base import BaseCommand
from django.conf import settings

class Command(BaseCommand):
    help = "Bridge PostgreSQL LISTEN/NOTIFY to Celery/Django Tasks"

    def handle(self, *args, **options):
        dsn = settings.DATABASES["default"]
        conn_string = f"host={dsn['HOST']} dbname={dsn['NAME']} user={dsn['USER']} password={dsn['PASSWORD']}"

        # Dedicated connection (not from pool!)
        conn = psycopg.connect(conn_string, autocommit=True)
        conn.execute("LISTEN settlement_events")
        conn.execute("LISTEN contract_events")

        self.stdout.write("Listening for PostgreSQL events...")

        for notify in conn.notifies():
            payload = json.loads(notify.payload)
            channel = notify.channel

            if channel == "settlement_events" and payload["action"] == "INSERT":
                generate_settlement_pdf.delay(payload["id"])
            elif channel == "contract_events" and payload["action"] == "INSERT":
                send_contract_notification.delay(payload["id"])
```

Create the trigger (see `references/apis-architecture.md` for the full trigger function):

```sql
CREATE TRIGGER settlement_notify
    AFTER INSERT OR UPDATE ON settlements
    FOR EACH ROW EXECUTE FUNCTION notify_change();
```

**Important:** LISTEN/NOTIFY does NOT work through PgBouncer in transaction mode. Use a dedicated non-pooled connection for the listener.

---

## 6. Monitoring and Reliability

### Celery Monitoring with Flower

```bash
pip install flower
celery -A proj flower --port=5555
```

Flower provides real-time monitoring: active/reserved/completed tasks, worker status, task details, and rate graphs.

### Task Retry Best Practices

```python
@shared_task(
    bind=True,
    max_retries=5,
    default_retry_delay=60,
    retry_backoff=True,        # exponential backoff
    retry_backoff_max=600,     # cap at 10 minutes
    retry_jitter=True,         # randomize to prevent thundering herd
    autoretry_for=(ConnectionError, TimeoutError),
)
def call_external_api(self, endpoint, payload):
    response = httpx.post(endpoint, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()
```

### Connection Management in Long-Running Tasks

Celery workers hold DB connections. For long-running tasks, explicitly close connections:

```python
from django.db import close_old_connections

@shared_task
def long_running_task():
    close_old_connections()  # close stale connections at start
    try:
        # ... long processing ...
        pass
    finally:
        close_old_connections()  # clean up after
```

Django automatically closes connections after each task (via `task_postrun` signal), but for tasks running > `CONN_MAX_AGE`, manual management is needed.

### Dead Letter Queue Pattern

For tasks that fail after all retries:

```python
@shared_task(bind=True, max_retries=3)
def process_webhook(self, event_id):
    try:
        event = WebhookEvent.objects.get(id=event_id)
        deliver_webhook(event)
    except MaxRetriesExceededError:
        # Move to dead letter queue for manual investigation
        FailedTask.objects.create(
            task_name="process_webhook",
            args={"event_id": event_id},
            error=traceback.format_exc(),
        )
```

### Key Libraries

| Library | Purpose | URL |
|---------|---------|-----|
| celery | Distributed task queue | https://docs.celeryq.dev/ |
| django-celery-results | Store results in Django DB | https://pypi.org/project/django-celery-results/ |
| django-celery-beat | Periodic tasks in DB | https://pypi.org/project/django-celery-beat/ |
| flower | Celery monitoring | https://flower.readthedocs.io/ |
| django-tasks | Django 6.0 DB backend | https://github.com/realOrangeOne/django-tasks |
| django-tasks-local | Lightweight thread backend | https://github.com/lincolnloop/django-tasks-local |
