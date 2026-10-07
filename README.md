# SPEEDERS ERP

SPEEDERS ERP is a Django-based Enterprise Resource Planning system for a sneaker
(sport shoes) manufacturer, covering procurement, production, inventory, quality,
distribution/sales, and an AI-assisted document/chat module.

## Modules

- **core** – authentication, role-based portals (factory/customer), shared UI (theme, navigation), middleware.
- **catalog** – product models and variants.
- **inventory** – warehouses, stock, lots, stock movements.
- **procurement** – suppliers, purchase requests, purchase orders.
- **production** – production orders, costing (material, labor, machine, overhead, scrap).
- **quality** – inbound acceptance/partial acceptance, scrap and rework tracking.
- **distribution** – customers, sales orders, invoices, shipments.
- **ai** – document upload, chunking, embeddings (pgvector) and semantic search, OpenAI-based chat assistant with ERP tool-calling.

## Tech stack

- Python 3.12, Django 5.2
- PostgreSQL 16 with the `pgvector` extension
- Redis (Celery broker/result backend) with Celery worker + beat
- Sentry for error monitoring, Prometheus/Grafana for metrics
- Local embedding model: `multilingual-e5-small`

## Language

The application interface is English-only. The historical Turkish/English
language toggle has been removed; `LANGUAGE_CODE` is fixed to `"en"`.

## Running locally (Windows, no Docker)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\local.ps1 start
```

Use `stop` or `status` as the last argument to stop the stack or check its
state. Restart (`stop` then `start`) after code changes. See `LOCAL-KURULUM.md`
for the full local setup reference (PostgreSQL/pgvector, Redis-compatible
Memurai, Celery workers, embedding pipeline, demo data).

To seed demo data in a local `DEBUG=True` environment:

```powershell
$env:DJANGO_SETTINGS_MODULE = 'config.settings_local'
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_demo_data
```

## Running with Docker Compose

```bash
docker compose up -d
```

This starts the Django app, Celery worker, Celery beat, PostgreSQL, Redis,
Prometheus, node-exporter and Grafana. Configure environment variables in a
`.env` file at the project root (see `docker-compose.yml` for the services
that read it).

## Tests

```bash
python manage.py test
```

## Documentation

- `LOCAL-KURULUM.md` – detailed local environment setup (Windows).
- `docs/DEMO_DATA.md` – demo data reference.
- `docs/THEMES.md` – theming reference.
