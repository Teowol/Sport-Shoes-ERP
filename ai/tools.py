"""Read-only ERP tools exposed to the AI assistant.

This module is the single authority for tool availability and data visibility.
Tool payloads deliberately use business identifiers (codes and order numbers),
never primary keys, costs, supplier prices, margins, credentials, or hashes.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import DatabaseError
from django.db.models import Q

from ai.permissions import can_search_documents
from ai.services.document_search import DEFAULT_SEARCH_LIMIT, search_document_chunks
from ai.services.local_embeddings import LocalEmbeddingError, LocalEmbeddingInputError
from distribution.models import SalesOrder
from inventory.models import Lot, Product, Stock
from production.models import ProductionOrder
from quality.models import QualityCheck


ROLE_BUYER = "buyer"
ROLE_FACTORY = "factory"

# Each tool declares its allowed roles here.  Keep this map as the source of
# truth when adding a future tool.
TOOL_REQUIRED_ROLES = {
    "search_products": {ROLE_BUYER, ROLE_FACTORY},
    "get_stock_by_product": {ROLE_FACTORY},
    "get_lot_details": {ROLE_FACTORY},
    "get_production_orders": {ROLE_FACTORY},
    "get_sales_orders": {ROLE_BUYER, ROLE_FACTORY},
    "get_fire_records": {ROLE_FACTORY},
    "search_documents": {ROLE_FACTORY},
}

MAX_LIMIT = 50


def get_user_role(user) -> str | None:
    """Return the single assistant role used for data-access decisions."""
    if not getattr(user, "is_authenticated", False):
        return None
    if user.is_superuser or user.is_staff or user.groups.filter(name="FactoryOwner").exists():
        return ROLE_FACTORY
    if user.groups.filter(name="Buyer").exists():
        return ROLE_BUYER
    return None


def is_ai_user_allowed(user) -> bool:
    return get_user_role(user) is not None


def _blocked(tool_name: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "access_denied",
        "tool": tool_name,
        "data": [],
    }


def _authorized(user, tool_name: str) -> bool:
    return get_user_role(user) in TOOL_REQUIRED_ROLES[tool_name]


def _normalize_limit(limit: Any) -> int:
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        return 10
    return max(1, min(parsed, MAX_LIMIT))


def _normalize_status(status: Any, choices) -> str | None:
    if status in (None, ""):
        return None
    valid_statuses = {value for value, _label in choices}
    return status if status in valid_statuses else None


def _json_value(value: Any) -> Any:
    """Convert values that JSON cannot natively encode to strings."""
    if isinstance(value, (Decimal, date, datetime)):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _success(tool_name: str, data: Any) -> dict[str, Any]:
    return {"ok": True, "tool": tool_name, "data": _json_value(data)}


def search_products(user, query: str) -> dict[str, Any]:
    """Search products without returning stock, price, or internal identifiers."""
    tool_name = "search_products"
    if not _authorized(user, tool_name):
        return _blocked(tool_name)

    query = (query or "").strip()
    if not query:
        return _success(tool_name, [])

    queryset = Product.objects.select_related("category").filter(is_active=True)
    if get_user_role(user) == ROLE_BUYER:
        queryset = queryset.filter(product_type=Product.ProductType.FINISHED_GOOD)

    products = queryset.filter(
        Q(code__icontains=query) | Q(name__icontains=query)
    ).order_by("code")[:MAX_LIMIT]
    return _success(
        tool_name,
        [
            {
                "code": product.code,
                "name": product.name,
                "category": product.category.name if product.category else None,
                "product_type": product.product_type,
                "unit": product.unit,
            }
            for product in products
        ],
    )


def get_stock_by_product(user, code_or_name: str) -> dict[str, Any]:
    """Return warehouse-level quantities for a product to factory users only."""
    tool_name = "get_stock_by_product"
    if not _authorized(user, tool_name):
        return _blocked(tool_name)

    value = (code_or_name or "").strip()
    if not value:
        return _success(tool_name, [])

    stocks = (
        Stock.objects.select_related("product", "warehouse")
        .filter(Q(product__code__iexact=value) | Q(product__name__icontains=value))
        .order_by("product__code", "warehouse__code")
    )
    return _success(
        tool_name,
        [
            {
                "product_code": stock.product.code,
                "product_name": stock.product.name,
                "unit": stock.product.unit,
                "warehouse_code": stock.warehouse.code,
                "warehouse_name": stock.warehouse.name,
                "quantity": stock.quantity,
                "reserved_quantity": stock.reserved_quantity,
                "available_quantity": stock.available_quantity,
                "updated_at": stock.updated_at,
            }
            for stock in stocks
        ],
    )


def get_lot_details(user, lot_code: str) -> dict[str, Any]:
    """Return a lot's traceability fields without source IDs or internal notes."""
    tool_name = "get_lot_details"
    if not _authorized(user, tool_name):
        return _blocked(tool_name)

    lot = Lot.resolve_by_identifier(lot_code)
    if not lot:
        return _success(tool_name, [])
    return _success(
        tool_name,
        {
            "lot_number": lot.lot_number,
            "product_code": lot.product.code,
            "product_name": lot.product.name,
            "unit": lot.product.unit,
            "status": lot.status,
            "manufactured_date": lot.manufactured_date,
            "expiry_date": lot.expiry_date,
            "initial_quantity": lot.initial_quantity,
            "remaining_quantity": lot.remaining_quantity,
            "created_at": lot.created_at,
        },
    )


def get_production_orders(user, status: str | None = None, limit: int = 10) -> dict[str, Any]:
    tool_name = "get_production_orders"
    if not _authorized(user, tool_name):
        return _blocked(tool_name)

    queryset = ProductionOrder.objects.select_related("product", "production_line")
    normalized_status = _normalize_status(status, ProductionOrder.Status.choices)
    if status not in (None, "") and normalized_status is None:
        return _success(tool_name, [])
    if normalized_status:
        queryset = queryset.filter(status=normalized_status)

    orders = queryset.order_by("-created_at")[:_normalize_limit(limit)]
    return _success(
        tool_name,
        [
            {
                "order_number": order.order_number,
                "product_code": order.product.code,
                "product_name": order.product.name,
                "production_line": order.production_line.code,
                "status": order.status,
                "priority": order.priority,
                "planned_quantity": order.planned_quantity,
                "produced_quantity": order.produced_quantity,
                "scrapped_quantity": order.scrapped_quantity,
                "planned_start_date": order.planned_start_date,
                "planned_end_date": order.planned_end_date,
            }
            for order in orders
        ],
    )


def get_sales_orders(user, status: str | None = None, limit: int = 10) -> dict[str, Any]:
    tool_name = "get_sales_orders"
    if not _authorized(user, tool_name):
        return _blocked(tool_name)

    queryset = SalesOrder.objects.select_related("customer").prefetch_related("lines__product")
    normalized_status = _normalize_status(status, SalesOrder.Status.choices)
    if status not in (None, "") and normalized_status is None:
        return _success(tool_name, [])
    if normalized_status:
        queryset = queryset.filter(status=normalized_status)

    if get_user_role(user) == ROLE_BUYER:
        customer = getattr(user, "customer_profile", None)
        queryset = queryset.filter(customer=customer) if customer else queryset.none()

    orders = queryset.order_by("-created_at")[:_normalize_limit(limit)]
    return _success(
        tool_name,
        [
            {
                "order_number": order.order_number,
                "customer_code": order.customer.code,
                "customer_name": order.customer.name,
                "status": order.status,
                "requested_delivery_date": order.requested_delivery_date,
                "promised_delivery_date": order.promised_delivery_date,
                "created_at": order.created_at,
                "lines": [
                    {
                        "product_code": line.product.code,
                        "product_name": line.product.name,
                        "quantity": line.quantity,
                        "unit": line.product.unit,
                    }
                    for line in order.lines.all()
                ],
            }
            for order in orders
        ],
    )


def get_fire_records(user, limit: int = 10) -> dict[str, Any]:
    tool_name = "get_fire_records"
    if not _authorized(user, tool_name):
        return _blocked(tool_name)

    checks = (
        QualityCheck.objects.select_related("production_order", "production_order__product")
        .filter(scrapped_quantity__gt=0)
        .order_by("-check_date")[:_normalize_limit(limit)]
    )
    return _success(
        tool_name,
        [
            {
                "production_order_number": check.production_order.order_number,
                "product_code": check.production_order.product.code,
                "product_name": check.production_order.product.name,
                "check_date": check.check_date,
                "checked_quantity": check.checked_quantity,
                "rejected_quantity": check.rejected_quantity,
                "scrapped_quantity": check.scrapped_quantity,
                "result": check.result,
                "rejection_reason": check.rejection_reason,
            }
            for check in checks
        ],
    )


def search_documents(user, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> dict[str, Any]:
    """Search private documents with a separate, stricter document access guard."""
    tool_name = "search_documents"
    if not _authorized(user, tool_name) or not can_search_documents(user):
        return _blocked(tool_name)
    try:
        return _success(tool_name, search_document_chunks(user, query, limit))
    except PermissionDenied:
        return _blocked(tool_name)
    except LocalEmbeddingInputError:
        return {"ok": False, "error": "invalid_tool_arguments", "tool": tool_name, "data": []}
    except (LocalEmbeddingError, DatabaseError, OSError):
        return {"ok": False, "error": "tool_unavailable", "tool": tool_name, "data": []}


TOOL_FUNCTIONS = {
    "search_products": search_products,
    "get_stock_by_product": get_stock_by_product,
    "get_lot_details": get_lot_details,
    "get_production_orders": get_production_orders,
    "get_sales_orders": get_sales_orders,
    "get_fire_records": get_fire_records,
    "search_documents": search_documents,
}
