-- Development-only Compose credentials.  Shared deployments must provision
-- their own runtime role and inject a secret rather than reusing these values.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'odp_app') THEN
        CREATE ROLE odp_app LOGIN PASSWORD 'odp_app_dev' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$$;

REVOKE ALL ON DATABASE odp FROM PUBLIC;
GRANT CONNECT ON DATABASE odp TO odp_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO odp_app;
