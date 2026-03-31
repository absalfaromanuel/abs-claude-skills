# Security, Roles & Permissions

## Table of Contents
1. Row Level Security (RLS)
2. Roles and Grants
3. Production Hardening Checklist
4. Encryption
5. Auditing and Logging
6. Django-Specific Security

---

## 1. Row Level Security (RLS)

RLS restricts which rows a user can see/modify via per-row policies. It's **fail-closed**: if no permissive policy exists, all rows are blocked.

### Setup

```sql
-- Enable RLS on table
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;

-- CRITICAL: Force RLS even for table owner (otherwise owner bypasses it)
ALTER TABLE projects FORCE ROW LEVEL SECURITY;

-- Create policy using session variable set by Django middleware
CREATE POLICY tenant_isolation ON projects
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);
```

`USING` controls which rows are visible (SELECT, UPDATE, DELETE).
`WITH CHECK` controls which rows can be written (INSERT, UPDATE).

### Policy Types

- **Permissive** (default): Multiple permissive policies combine with OR
- **Restrictive**: Combine with AND, always applied on top of permissive policies

```sql
-- Permissive: user sees their tenant's data
CREATE POLICY tenant_access ON documents
    AS PERMISSIVE FOR ALL
    USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

-- Restrictive: additionally, only see non-deleted documents
CREATE POLICY not_deleted ON documents
    AS RESTRICTIVE FOR SELECT
    USING (deleted_at IS NULL);
```

### Common Footguns

1. **Superusers and table owners bypass RLS** — always use `FORCE ROW LEVEL SECURITY` and test with non-superuser accounts.

2. **Views bypass RLS by default** — in PostgreSQL 15+, use:
   ```sql
   CREATE VIEW my_view WITH (security_invoker = true) AS SELECT ...;
   ```

3. **Connection pooling (PgBouncer)** — use `SET LOCAL` (transaction-scoped), never `SET` (session-scoped):
   ```python
   cursor.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
   ```

4. **Backup/restore** — `pg_dump` runs as superuser and bypasses RLS. This is correct behavior.

5. **Missing index on policy column** — always index columns used in RLS policies:
   ```sql
   CREATE INDEX idx_projects_tenant ON projects (tenant_id);
   ```

6. **`current_setting` without `true` as second arg** — throws error if variable isn't set instead of returning NULL. Always use `current_setting('var', true)`.

### Django Middleware for RLS

```python
from django.db import connection

class TenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant_id = self._get_tenant(request)
        if tenant_id:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SET LOCAL app.current_tenant_id = %s",
                    [str(tenant_id)]
                )
        response = self.get_response(request)
        return response

    def _get_tenant(self, request):
        # Extract from JWT, session, subdomain, etc.
        if hasattr(request, 'user') and hasattr(request.user, 'tenant_id'):
            return request.user.tenant_id
        return None
```

**Library:** `django-rls-tenants` provides this pattern with fail-closed design.

**Docs:** https://www.postgresql.org/docs/current/ddl-rowsecurity.html

---

## 2. Roles and Grants

In PostgreSQL, only **roles** exist. `CREATE USER` is an alias for `CREATE ROLE WITH LOGIN`.

### Recommended Role Hierarchy for Django SaaS

```sql
-- 1. Group roles (no login)
CREATE ROLE app_reader NOLOGIN;
CREATE ROLE app_writer NOLOGIN;
CREATE ROLE app_migrator NOLOGIN;

-- 2. Grant schema access
GRANT USAGE ON SCHEMA public TO app_reader;
GRANT USAGE ON SCHEMA public TO app_writer;
GRANT ALL ON SCHEMA public TO app_migrator;

-- 3. Grant table access
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_writer;
GRANT ALL ON ALL TABLES IN SCHEMA public TO app_migrator;

-- 4. Default privileges for future tables
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO app_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO app_migrator;

-- 5. Grant sequence access (for auto-increment)
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO app_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE ON SEQUENCES TO app_writer;

-- 6. Login roles that inherit group permissions
CREATE ROLE django_app LOGIN PASSWORD 'strong_password' IN ROLE app_writer;
CREATE ROLE django_migrate LOGIN PASSWORD 'strong_password' IN ROLE app_migrator;
CREATE ROLE django_readonly LOGIN PASSWORD 'strong_password' IN ROLE app_reader;
```

### Django Configuration

```python
# settings.py
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "USER": os.environ["DB_USER"],        # django_app
        "PASSWORD": os.environ["DB_PASSWORD"],
        # ...
    },
    "readonly": {
        "ENGINE": "django.db.backends.postgresql",
        "USER": os.environ["DB_READONLY_USER"],  # django_readonly
        "PASSWORD": os.environ["DB_READONLY_PASSWORD"],
        # point to read replica for read scaling
        "HOST": os.environ.get("DB_READONLY_HOST", os.environ["DB_HOST"]),
    },
}
```

### Revoke Public Access

```sql
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE mydb FROM PUBLIC;
```

**Docs:** https://www.postgresql.org/docs/current/ddl-priv.html, https://www.postgresql.org/docs/current/sql-grant.html

---

## 3. Production Hardening Checklist

Apply all of these before going to production:

### Authentication

- [ ] Change default `postgres` password
- [ ] Use `scram-sha-256` (not `md5`) in `pg_hba.conf`
- [ ] Disable `trust` authentication method
- [ ] Restrict `pg_hba.conf` to specific IP ranges
- [ ] Use SSL: `hostssl` entries only (no `host`)
- [ ] Set `password_encryption = 'scram-sha-256'`

### Network

- [ ] Set `listen_addresses` to specific IPs (never `*` without firewall)
- [ ] Enable SSL: `ssl = on`, `ssl_min_protocol_version = 'TLSv1.2'`
- [ ] Use certificates: `ssl_cert_file`, `ssl_key_file`, `ssl_ca_file`
- [ ] Firewall: only allow port 5432 from app servers

### Connection Safety

- [ ] Set `idle_in_transaction_session_timeout = '30s'`
- [ ] Set `statement_timeout = '30s'` (prevent runaway queries)
- [ ] Set `tcp_keepalives_idle = 60`
- [ ] Use connection pooling (PgBouncer or Django 5.1+ native)

### Data Integrity

- [ ] Enable data checksums: `initdb --data-checksums`
- [ ] Configure WAL archiving for point-in-time recovery
- [ ] Test backup restoration regularly
- [ ] Enable `log_checkpoints = on`

### Logging

- [ ] Set `log_connections = on`, `log_disconnections = on`
- [ ] Set `log_min_duration_statement = 1000` (log queries >1s)
- [ ] Set `log_line_prefix = '%t [%p]: user=%u,db=%d,app=%a '`
- [ ] Enable `pgaudit` for compliance logging

### Updates

- [ ] Subscribe to `pgsql-announce` mailing list
- [ ] Apply security patches within 30 days
- [ ] Keep PostgreSQL on a supported major version

**Reference:** CIS PostgreSQL Benchmark (https://www.cisecurity.org/benchmark/postgresql)
**Tool:** pgdsat — automated security assessment (https://github.com/HexaCluster/pgdsat)

**Docs:** https://www.postgresql.org/docs/current/auth-pg-hba-conf.html, https://www.postgresql.org/docs/current/ssl-tcp.html

---

## 4. Encryption

### In Transit

Always enforce SSL from Django:
```python
DATABASES = {
    "default": {
        "OPTIONS": {
            "sslmode": "verify-full",
            "sslrootcert": "/path/to/ca-certificate.crt",
        },
    }
}
```

On managed services (RDS, Supabase), SSL is enabled by default. Use `sslmode=verify-full` for production.

### At Rest

**Full disk encryption** (recommended): Use LUKS (Linux), AWS EBS encryption, or GCP CMEK. Transparent to PostgreSQL, no application changes needed.

**Column-level encryption** with `pgcrypto`:
```sql
CREATE EXTENSION pgcrypto;

-- Symmetric encryption
INSERT INTO sensitive_data (ssn_encrypted)
VALUES (pgp_sym_encrypt('123-45-6789', 'encryption_key'));

-- Decryption
SELECT pgp_sym_decrypt(ssn_encrypted, 'encryption_key') FROM sensitive_data;
```

**Never store encryption keys in the database.** Use AWS KMS, HashiCorp Vault, or environment variables.

Column-level encryption prevents indexing on encrypted columns. Use it only for highly sensitive fields (SSN, health data, payment info).

**Docs:** https://www.postgresql.org/docs/current/pgcrypto.html

---

## 5. Auditing and Logging

### pgAudit

Provides detailed session and object audit logging required for SOC 2, HIPAA, GDPR compliance.

```sql
-- Install
CREATE EXTENSION pgaudit;

-- Configure
ALTER SYSTEM SET pgaudit.log = 'read, write, ddl, role';
ALTER SYSTEM SET pgaudit.log_catalog = off;
ALTER SYSTEM SET pgaudit.log_relation = on;
ALTER SYSTEM SET pgaudit.log_statement_once = on;
SELECT pg_reload_conf();
```

Levels:
- `read`: SELECT, COPY TO
- `write`: INSERT, UPDATE, DELETE, TRUNCATE, COPY FROM
- `ddl`: CREATE, ALTER, DROP
- `role`: GRANT, REVOKE, CREATE/ALTER/DROP ROLE

Send audit logs to a SIEM (ELK, Splunk, CloudWatch Logs) for retention and alerting.

### Django-Level Auditing

**django-pghistory** creates audit trails via PostgreSQL triggers:
```python
import pghistory

@pghistory.track(pghistory.Snapshot())
class Order(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20)
```

Every INSERT/UPDATE auto-generates a snapshot with user context and timestamp.

**Libraries:**
- pgAudit: https://github.com/pgaudit/pgaudit
- django-pghistory: https://github.com/AmbitionEng/django-pghistory

---

## 6. Django-Specific Security

### Connection String Security

```python
# NEVER hardcode credentials
DATABASES = {
    "default": {
        "USER": os.environ["DB_USER"],
        "PASSWORD": os.environ["DB_PASSWORD"],
        # Or use django-environ, python-decouple, AWS Secrets Manager
    }
}
```

### SQL Injection Prevention

Django ORM parameterizes queries automatically. psycopg3 adds **structural protection** via
server-side binding — query and parameters are sent separately to PostgreSQL.

```python
# SAFE — parameterized (psycopg3 sends separately via extended protocol)
cursor.execute("SELECT * FROM users WHERE email = %s", [email])

# SAFE — ORM
User.objects.filter(email=email)

# DANGEROUS — string interpolation
cursor.execute(f"SELECT * FROM users WHERE email = '{email}'")  # SQL INJECTION!
```

### `psycopg.sql` — Safe Dynamic Identifiers

Table/column/schema names **cannot** be parameterized. Use `psycopg.sql` to escape them safely:

```python
from psycopg import sql

# Multi-tenant schema-based queries:
query = sql.SQL("SELECT * FROM {schema}.{table} WHERE id = %s").format(
    schema=sql.Identifier(tenant_schema),
    table=sql.Identifier("users"),
)
cursor.execute(query, (user_id,))
# → SELECT * FROM "tenant_42"."users" WHERE id = $1

# Dynamic INSERT:
def safe_insert(conn, table, data: dict):
    query = sql.SQL("INSERT INTO {table} ({fields}) VALUES ({placeholders})").format(
        table=sql.Identifier(table),
        fields=sql.SQL(", ").join(map(sql.Identifier, data.keys())),
        placeholders=sql.SQL(", ").join(sql.Placeholder() * len(data)),
    )
    conn.execute(query, list(data.values()))
```

**Key rule:** Never use f-strings or `.format()` for SQL. Always `psycopg.sql` for identifiers, `%s` for values.

### Django Security Middleware

Ensure these are enabled:
```python
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # ...
]

SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
```
