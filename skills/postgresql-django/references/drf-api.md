# Django REST Framework + PostgreSQL

## Table of Contents
1. ViewSet and Queryset Optimization
2. Serializer Performance
3. Pagination for Large Tables
4. Filtering and Search
5. Caching API Responses
6. Throttling and Rate Limiting
7. Authentication Patterns for SaaS
8. Profiling and Debugging

---

## 1. ViewSet and Queryset Optimization

The single most important rule for DRF performance: **optimize the queryset in `get_queryset()`, not in the serializer.** The serializer should receive already-optimized data.

```python
from rest_framework import viewsets, filters
from django_filters.rest_framework import DjangoFilterBackend

class EmployeeViewSet(viewsets.ModelViewSet):
    serializer_class = EmployeeSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["status", "department"]
    search_fields = ["first_name", "paternal_surname", "curp"]
    ordering_fields = ["hire_date", "created_at"]
    ordering = ["-created_at"]

    def get_queryset(self):
        # 1. Always filter by tenant (complements RLS as defense-in-depth)
        qs = Employee.objects.filter(tenant=self.request.user.tenant)

        # 2. select_related for FK accessed in serializer
        qs = qs.select_related("tenant")

        # 3. prefetch_related for reverse FK / M2M
        qs = qs.prefetch_related(
            Prefetch(
                "contracts",
                queryset=Contract.objects.filter(status="active").only("id", "contract_type", "status"),
                to_attr="active_contracts",
            )
        )

        # 4. Annotate computed fields at DB level (avoid N+1 in serializer)
        qs = qs.annotate(
            contract_count=Count("contracts", filter=Q(contracts__status="active")),
        )
        return qs
```

### Use Different Serializers for List vs Detail

List views don't need all fields. Use separate serializers to minimize data transfer and DB load:

```python
class EmployeeViewSet(viewsets.ModelViewSet):
    def get_serializer_class(self):
        if self.action == "list":
            return EmployeeListSerializer   # minimal fields
        return EmployeeDetailSerializer      # full fields with nested relations
```

### Use `only()` and `defer()` in ViewSets

```python
def get_queryset(self):
    if self.action == "list":
        return Employee.objects.filter(
            tenant=self.request.user.tenant
        ).only("id", "uuid", "first_name", "paternal_surname", "status", "position")
    return Employee.objects.filter(tenant=self.request.user.tenant)
```

**Docs:** https://www.django-rest-framework.org/api-guide/viewsets/, https://docs.djangoproject.com/en/6.0/topics/db/optimization/

---

## 2. Serializer Performance

### Avoid N+1 in Serializers

The serializer should **never trigger additional queries**. All related data must be pre-fetched in the queryset.

```python
class EmployeeListSerializer(serializers.ModelSerializer):
    # Source from annotation (0 extra queries)
    contract_count = serializers.IntegerField(read_only=True)
    # Source from select_related FK (0 extra queries)
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)

    class Meta:
        model = Employee
        fields = ["uuid", "first_name", "paternal_surname", "status",
                  "position", "contract_count", "tenant_name"]
        read_only_fields = ["uuid"]


class EmployeeDetailSerializer(serializers.ModelSerializer):
    # Nested serializer from prefetch_related (0 extra queries)
    active_contracts = ContractSummarySerializer(many=True, read_only=True)

    class Meta:
        model = Employee
        fields = "__all__"
```

### SerializerMethodField: Use with Caution

`SerializerMethodField` executes Python per-row. If it accesses related objects without prefetch, it causes N+1:

```python
# BAD: N+1 — triggers 1 query per employee
class EmployeeSerializer(serializers.ModelSerializer):
    latest_contract = serializers.SerializerMethodField()

    def get_latest_contract(self, obj):
        return obj.contracts.order_by("-created_at").first().contract_type  # N+1!

# GOOD: Use annotation in queryset + read from annotated field
class EmployeeSerializer(serializers.ModelSerializer):
    latest_contract_type = serializers.CharField(read_only=True)  # from Subquery annotation
```

### Write vs Read Serializers

Separate serializers for input (write) and output (read) is a common and recommended pattern:

```python
class ContractWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Contract
        fields = ["employee", "contract_type", "period", "agreed_salary",
                  "payment_frequency", "additional_clauses"]

class ContractReadSerializer(serializers.ModelSerializer):
    employee = EmployeeListSerializer(read_only=True)

    class Meta:
        model = Contract
        fields = "__all__"

class ContractViewSet(viewsets.ModelViewSet):
    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return ContractWriteSerializer
        return ContractReadSerializer
```

**Docs:** https://www.django-rest-framework.org/api-guide/serializers/

---

## 3. Pagination for Large Tables

### CursorPagination (recommended for large datasets)

Uses keyset pagination under the hood — O(1) performance regardless of dataset size. No `COUNT(*)` query, no OFFSET.

```python
from rest_framework.pagination import CursorPagination

class TimelinePagination(CursorPagination):
    page_size = 20
    ordering = "-created_at"       # must be an indexed, sequential field
    cursor_query_param = "cursor"

class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    pagination_class = TimelinePagination
    serializer_class = AuditLogSerializer

    def get_queryset(self):
        return AuditLog.objects.filter(tenant=self.request.user.tenant)
```

Requirements for `CursorPagination`:
- Ordering field must be **indexed**, **sequential** (timestamps, auto-increment IDs), and **non-nullable**
- Cannot use arbitrary ordering (users can't sort by any field)
- Ideal for feeds, timelines, logs, and event streams

### PageNumberPagination (for small/medium datasets)

Simpler but runs `COUNT(*)` per request — expensive on millions of rows:

```python
from rest_framework.pagination import PageNumberPagination

class StandardPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100
```

### Decision Guide

| Dataset Size | Pagination Type | Why |
|-------------|----------------|-----|
| < 10K rows | `PageNumberPagination` | Simple, user-friendly page numbers |
| 10K - 1M rows | `LimitOffsetPagination` | Flexible, but watch OFFSET degradation |
| > 1M rows | `CursorPagination` | O(1) performance, no COUNT(*) |

**Docs:** https://www.django-rest-framework.org/api-guide/pagination/

---

## 4. Filtering and Search

### django-filter for Structured Filtering

```python
# pip install django-filter
import django_filters

class SettlementFilter(django_filters.FilterSet):
    effective_date_after = django_filters.DateFilter(field_name="effective_date", lookup_expr="gte")
    effective_date_before = django_filters.DateFilter(field_name="effective_date", lookup_expr="lte")
    min_amount = django_filters.NumberFilter(field_name="total_amount", lookup_expr="gte")

    class Meta:
        model = Settlement
        fields = ["settlement_type", "status", "termination_reason"]

class SettlementViewSet(viewsets.ModelViewSet):
    filterset_class = SettlementFilter
```

Ensure every filtered field has an appropriate index. Check with `EXPLAIN ANALYZE`.

### Full-Text Search with PostgreSQL

```python
from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank

class EmployeeViewSet(viewsets.ModelViewSet):
    def get_queryset(self):
        qs = Employee.objects.filter(tenant=self.request.user.tenant)
        search = self.request.query_params.get("q")
        if search:
            vector = SearchVector("first_name", "paternal_surname", "curp", config="spanish")
            query = SearchQuery(search, config="spanish")
            qs = qs.annotate(rank=SearchRank(vector, query)).filter(rank__gte=0.1).order_by("-rank")
        return qs
```

For better performance, pre-compute `SearchVectorField` and add a GIN index (see `references/django-orm.md`).

**Docs:** https://www.django-rest-framework.org/api-guide/filtering/, https://django-filter.readthedocs.io/

---

## 5. Caching API Responses

### View-Level Caching

```python
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_headers

class PlanListViewSet(viewsets.ReadOnlyModelViewSet):
    """Plans don't change often — cache for 15 minutes."""
    serializer_class = PlanSerializer
    queryset = Plan.objects.filter(is_active=True)

    @method_decorator(cache_page(60 * 15))
    @method_decorator(vary_on_headers("Authorization"))
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)
```

### Object-Level Caching

```python
from django.core.cache import cache

class EmployeeViewSet(viewsets.ModelViewSet):
    def retrieve(self, request, *args, **kwargs):
        cache_key = f"employee:{kwargs['pk']}:tenant:{request.user.tenant_id}"
        data = cache.get(cache_key)
        if data is None:
            response = super().retrieve(request, *args, **kwargs)
            cache.set(cache_key, response.data, timeout=300)
            return response
        return Response(data)
```

### ETag / Conditional Requests

DRF supports ETags for conditional caching:
```python
# pip install djangorestframework-condition
from rest_framework_condition import etag

def employee_etag(request, *args, **kwargs):
    return str(Employee.objects.filter(
        tenant=request.user.tenant
    ).aggregate(Max("updated_at"))["updated_at__max"])

class EmployeeListView(generics.ListAPIView):
    @etag(employee_etag)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)
```

**Docs:** https://www.django-rest-framework.org/api-guide/caching/

---

## 6. Throttling and Rate Limiting

```python
# settings.py
REST_FRAMEWORK = {
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "100/hour",
        "user": "1000/hour",
    },
}

# Per-view throttling for expensive endpoints
from rest_framework.throttling import UserRateThrottle

class SettlementCalculationThrottle(UserRateThrottle):
    rate = "10/minute"

class SettlementCalculateView(generics.CreateAPIView):
    throttle_classes = [SettlementCalculationThrottle]
```

**Docs:** https://www.django-rest-framework.org/api-guide/throttling/

---

## 7. Authentication Patterns for SaaS

### JWT + Tenant Context

```python
# pip install djangorestframework-simplejwt
from rest_framework_simplejwt.authentication import JWTAuthentication

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}
```

### Custom Permission for Tenant Isolation

```python
from rest_framework.permissions import BasePermission

class IsTenantMember(BasePermission):
    """Verify user belongs to the tenant referenced in the request."""
    def has_object_permission(self, request, view, obj):
        if hasattr(obj, "tenant_id"):
            return obj.tenant_id == request.user.tenant_id
        return True
```

### Multi-Tenant Mixin

```python
class TenantViewMixin:
    """Auto-filter queryset by tenant and set tenant on create."""
    def get_queryset(self):
        return super().get_queryset().filter(tenant=self.request.user.tenant)

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.user.tenant)

class ContractViewSet(TenantViewMixin, viewsets.ModelViewSet):
    serializer_class = ContractSerializer
    queryset = Contract.objects.all()
```

**Docs:** https://www.django-rest-framework.org/api-guide/authentication/, https://www.django-rest-framework.org/api-guide/permissions/

---

## 8. Profiling and Debugging

### django-debug-toolbar (development)

Shows SQL queries per request. Essential for catching N+1 in DRF views.

### django-silk (staging/production-safe)

Records request time, query count, and query time per endpoint. Can be enabled temporarily for profiling.

### DRF's Built-in Query Logging

```python
# In Django shell or test
from django.db import connection, reset_queries
from django.conf import settings

settings.DEBUG = True
reset_queries()

# Execute your view/serializer code here
response = client.get("/api/employees/")

print(f"Queries: {len(connection.queries)}")
for q in connection.queries:
    print(f"  [{q['time']}s] {q['sql'][:100]}")
```

### Load Testing

Use `locust` or `wrk` to benchmark endpoints under load:
```python
# locustfile.py
from locust import HttpUser, task

class APIUser(HttpUser):
    @task
    def list_employees(self):
        self.client.get("/api/employees/", headers={"Authorization": f"Bearer {TOKEN}"})

    @task
    def get_employee(self):
        self.client.get("/api/employees/1/", headers={"Authorization": f"Bearer {TOKEN}"})
```

**Libraries:**
- django-debug-toolbar: https://github.com/jazzband/django-debug-toolbar
- django-silk: https://github.com/jazzband/django-silk
- locust: https://locust.io/
- DRF Official: https://www.django-rest-framework.org/
