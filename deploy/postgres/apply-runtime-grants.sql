-- Re-run after every owner/migrator migration.  This is deliberately
-- idempotent and keeps the runtime role unable to rewrite audit evidence.
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM odp_app;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM odp_app;
GRANT USAGE ON SCHEMA public TO odp_app;

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'actors', 'actor_line_grants', 'password_credentials', 'defect_cases',
        'inspection_events', 'case_transitions', 'alerts', 'inspection_alerts',
        'websocket_tickets', 'reauthentication_markers', 'knowledge_documents',
        'knowledge_parent_chunks', 'knowledge_chunk_index'
    ]
    LOOP
        IF to_regclass('public.' || table_name) IS NOT NULL THEN
            EXECUTE format(
                'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO odp_app',
                table_name
            );
        END IF;
    END LOOP;
END
$$;

GRANT SELECT, INSERT ON TABLE public.audit_logs TO odp_app;
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.audit_logs FROM odp_app;
GRANT SELECT, INSERT, UPDATE ON TABLE public.audit_chain_heads TO odp_app;
REVOKE DELETE, TRUNCATE ON TABLE public.audit_chain_heads FROM odp_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO odp_app;
