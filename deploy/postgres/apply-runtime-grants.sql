-- Re-run after every owner/migrator migration.  This is deliberately
-- idempotent and keeps the runtime role unable to rewrite audit evidence.
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM odp_app;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM odp_app;
GRANT USAGE ON SCHEMA public TO odp_app;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM odp_api, odp_worker, odp_relay, odp_scheduler;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM odp_api, odp_worker, odp_relay, odp_scheduler;
GRANT USAGE ON SCHEMA public TO odp_api, odp_worker, odp_relay, odp_scheduler;

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

-- The API role retains the legacy app surface, including append-only audit
-- writes.  The old odp_app grants above remain for existing Compose volumes;
-- new deployments should use odp_api in the API container.
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
                'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO odp_api',
                table_name
            );
        END IF;
    END LOOP;
END
$$;

GRANT SELECT, INSERT ON TABLE public.audit_logs TO odp_api;
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.audit_logs FROM odp_api;
GRANT SELECT, INSERT, UPDATE ON TABLE public.audit_chain_heads TO odp_api;
REVOKE DELETE, TRUNCATE ON TABLE public.audit_chain_heads FROM odp_api;

-- Worker owns task claims and completion effects.  It intentionally has no
-- access to user/credential tables.  Audit writes remain append-only.
DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'camera_inference_state', 'inspection_sessions', 'frame_artifacts',
        'inference_tasks', 'inference_attempts', 'published_inference_results',
        'outbox_events', 'message_quarantine', 'defect_episode', 'inspection_events',
        'alerts', 'inspection_alerts'
    ]
    LOOP
        IF to_regclass('public.' || table_name) IS NOT NULL THEN
            EXECUTE format(
                'GRANT SELECT, INSERT, UPDATE ON TABLE public.%I TO odp_worker',
                table_name
            );
        END IF;
    END LOOP;
END
$$;
GRANT SELECT, INSERT ON TABLE public.audit_logs TO odp_worker;
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.audit_logs FROM odp_worker;
GRANT SELECT, INSERT, UPDATE ON TABLE public.audit_chain_heads TO odp_worker;
REVOKE DELETE, TRUNCATE ON TABLE public.audit_chain_heads FROM odp_worker;

-- Relay only claims and marks Outbox rows; it cannot mutate task results.
GRANT SELECT, UPDATE ON TABLE public.outbox_events TO odp_relay;

-- Scheduler/reconciler own recovery state and artifact lifecycle transitions.
DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'camera_inference_state', 'inspection_sessions', 'frame_artifacts',
        'inference_tasks', 'inference_attempts', 'outbox_events',
        'message_quarantine', 'defect_episode'
    ]
    LOOP
        IF to_regclass('public.' || table_name) IS NOT NULL THEN
            EXECUTE format(
                'GRANT SELECT, UPDATE ON TABLE public.%I TO odp_scheduler',
                table_name
            );
        END IF;
    END LOOP;
END
$$;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO odp_api, odp_worker, odp_relay, odp_scheduler;
