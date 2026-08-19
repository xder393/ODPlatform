# apps/web-backend

FastAPI service foundation for the ODPlatform quality-inspection platform.

## Run locally

Install this package and start the app with:

```bash
uvicorn odp_api.main:create_app --factory --reload
```

The service exposes `GET /healthz`, returning `{"status": "ok"}`. Runtime
configuration is provided by `Settings` and reads `ODP_`-prefixed environment
variables.

The Web service remains separate from `apps/platform`, which continues to be
the independent visual inspection core. Shared event contracts live in
`packages/shared-schemas`.
