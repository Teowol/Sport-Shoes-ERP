# SPEEDERS themes

## Shared structure

All application pages extend `core/base.html`. Existing page layouts remain in
their templates; page-specific styles are in `static/css/pages/`. The homepage
and existing ERP screens continue to use `home.css` and `erp-theme.css`.

`static/css/theme.css` owns the palette and shared interaction states. Light
mode uses warm off-white surfaces, navy text, and gold actions. Dark mode keeps
the navy/gold palette. Gold action fills and gold text use separate tokens so
text remains readable on light surfaces.

Use semantic variables such as `--surface-primary`, `--text-primary`,
`--text-secondary`, `--border-color`, `--input-border`, `--accent-primary`,
`--accent-text`, and the success/danger/warning/info tokens. Avoid new literal
theme-dependent colors. Compatibility aliases support older component styles.

## Controller and preference

`static/js/theme.js` runs synchronously in the head before content is displayed.
It reads `localStorage["speeders-theme"]`, accepting only `light` or `dark`.
Without a saved choice it follows `prefers-color-scheme`, including live system
changes. A manual choice overrides the system. Storage events synchronize open
tabs. If browser storage is blocked, switching still works for the current page.

```js
SpeedersTheme.setTheme("light");
SpeedersTheme.setTheme("dark");
SpeedersTheme.toggleTheme();
SpeedersTheme.getCurrentTheme();
```

The controller dispatches a `themechange` window event with
`event.detail.theme`. Future chart integrations can listen to that event and
read computed CSS variables; current dashboards use CSS progress indicators,
not a JavaScript chart library.

## Controls and Django admin

The shared base renders one `core/_theme_toggle.html` control. Mark the existing
navbar/settings container with `data-theme-toolbar`; the controller mounts the
control there. Pages without a toolbar get a small fixed fallback control.
Authentication pages place it beside the language selector.

Project overrides in `templates/admin/` replace Django's separate theme
controller with the same SPEEDERS control. `admin-theme.css` maps native Django
variables and styles the sidebar, autocomplete menus, calendar/time pickers,
forms, results tables, pagination, and related-object popup windows. The
template search directory in settings enables those overrides; authentication
and permissions are unchanged.

Theme and TR/EN preferences are independent. Toggle labels come from the
existing translation catalog. Without JavaScript the light palette remains
usable and the inactive theme button stays hidden.

## Assets and limitations

Sneaker/product images keep their original colors. Existing dark logo and hero
artwork retain dark insets on light pages. Browser-owned confirmation dialogs,
native select popups, and date pickers follow browser/OS rendering and the CSS
`color-scheme`; their exact appearance varies by browser. PDF invoices are
print documents and do not adopt the website theme.

After editing Python or templates, restart the local web process: `local.ps1`
currently launches Django with `--noreload` and cached template loading. Static
asset query versions should be bumped when changing shared theme assets.

## Files updated in this implementation

- `ai/templates/ai/chat.html`
- `catalog/templates/catalog/index.html`
- `catalog/templates/catalog/product_detail.html`
- `catalog/templates/catalog/product_list.html`
- `config/settings.py`
- `core/templates/core/_theme_toggle.html`
- `core/templates/core/base.html`
- `core/templates/core/customer_home.html`
- `core/templates/core/home.html`
- `core/templates/core/portal.html`
- `core/templates/registration/login.html`
- `core/templates/registration/register.html`
- `core/test_theme.py`
- `core/translations.py`
- `distribution/templates/distribution/customer_order_detail.html`
- `distribution/templates/distribution/customer_order_list.html`
- `distribution/templates/distribution/customer_purchase.html`
- `distribution/templates/distribution/customer_purchase_detail.html`
- `distribution/templates/distribution/sales_order_create.html`
- `distribution/templates/distribution/sales_order_detail.html`
- `distribution/templates/distribution/sales_order_edit.html`
- `distribution/templates/distribution/sales_order_list.html`
- `docs/THEMES.md`
- `inventory/templates/inventory/fire_tracking_list.html`
- `inventory/templates/inventory/lot_tracking_list.html`
- `inventory/templates/inventory/product_movement_history.html`
- `inventory/templates/inventory/stock_level_list.html`
- `inventory/templates/inventory/stock_movement_list.html`
- `procurement/templates/procurement/procurement.html`
- `production/templates/production/cost_list.html`
- `production/templates/production/operation_list.html`
- `production/templates/production/order_detail.html`
- `production/templates/production/order_list.html`
- `quality/templates/quality/quality_check_list.html`
- `static/css/admin-theme.css`
- `static/css/erp-theme.css`
- `static/css/home.css`
- `static/css/language-toggle.css`
- `static/css/pages/ai-chat.css`
- `static/css/pages/catalog-index.css`
- `static/css/pages/catalog-product_detail.css`
- `static/css/pages/catalog-product_list.css`
- `static/css/pages/core-customer_home.css`
- `static/css/pages/core-login.css`
- `static/css/pages/core-portal.css`
- `static/css/pages/core-register.css`
- `static/css/pages/distribution-customer_order_detail.css`
- `static/css/pages/distribution-customer_order_list.css`
- `static/css/pages/distribution-customer_purchase.css`
- `static/css/pages/distribution-customer_purchase_detail.css`
- `static/css/pages/inventory-fire_tracking_list.css`
- `static/css/pages/inventory-lot_tracking_list.css`
- `static/css/pages/procurement-procurement.css`
- `static/css/pages/production-cost_list.css`
- `static/css/pages/production-order_list.css`
- `static/css/theme.css`
- `static/js/theme.js`
- `templates/admin/base.html`
- `templates/admin/color_theme_toggle.html`
