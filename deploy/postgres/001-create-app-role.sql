-- Development-only Compose credentials.  Shared deployments must provision
-- their own runtime role and inject a secret rather than reusing these values.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'odp_app') THEN
        CREATE ROLE odp_app LOGIN PASSWORD 'odp_app_dev' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$$;

ALTER ROLE odp_app LOGIN PASSWORD 'odp_app_dev' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

-- P1 processes use separate login identities.  Keeping these roles explicit
-- (rather than inheriting from one shared app role) makes a leaked worker
-- credential unable to read or mutate API-only tables.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'odp_api') THEN
        CREATE ROLE odp_api LOGIN PASSWORD 'odp_api_dev' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'odp_worker') THEN
        CREATE ROLE odp_worker LOGIN PASSWORD 'odp_worker_dev' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'odp_relay') THEN
        CREATE ROLE odp_relay LOGIN PASSWORD 'odp_relay_dev' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'odp_scheduler') THEN
        CREATE ROLE odp_scheduler LOGIN PASSWORD 'odp_scheduler_dev' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$$;

ALTER ROLE odp_api LOGIN PASSWORD 'odp_api_dev' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE odp_worker LOGIN PASSWORD 'odp_worker_dev' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE odp_relay LOGIN PASSWORD 'odp_relay_dev' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE odp_scheduler LOGIN PASSWORD 'odp_scheduler_dev' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

REVOKE ALL ON DATABASE odp FROM PUBLIC;
GRANT CONNECT ON DATABASE odp TO odp_app;
GRANT CONNECT ON DATABASE odp TO odp_api, odp_worker, odp_relay, odp_scheduler;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO odp_app;
GRANT USAGE ON SCHEMA public TO odp_api, odp_worker, odp_relay, odp_scheduler;
