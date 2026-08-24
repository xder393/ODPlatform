import { useEffect, useState } from "react";

import { createWebSocketTicket, reconcileInspectionAlerts } from "../../api/client";
import type { InspectionAlert, InspectionAlertEnvelope } from "../../api/types";

export type { InspectionAlert } from "../../api/types";

const CURSOR_STORAGE_PREFIX = "odp_alert_cursor:";
const RECONNECT_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000] as const;
const STABLE_CONNECTION_MS = 5_000;
const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ISO_DATETIME_WITH_TIMEZONE = /^(\d{4})-(\d{2})-(\d{2})T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/i;

function actorScope(token: string): string {
  try {
    const payload = token.split(".")[1];
    if (!payload) throw new Error("JWT payload is missing");
    const normalized = payload.replace(/-/g, "+").replace(/_/g, "/");
    const subject = JSON.parse(atob(normalized)) as { sub?: unknown };
    if (typeof subject.sub === "string" && subject.sub.length > 0) return subject.sub;
  } catch {
    // Invalid tokens are rejected by the API. Keep their cursor isolated so a
    // malformed replacement cannot inherit an authenticated actor's cursor.
  }
  let hash = 2166136261;
  for (const character of token) hash = Math.imul(hash ^ character.charCodeAt(0), 16777619);
  return `session-${(hash >>> 0).toString(16)}`;
}

/** Return the actor/session-isolated key without exposing raw token material. */
export function inspectionAlertCursorStorageKey(token: string): string {
  return `${CURSOR_STORAGE_PREFIX}${actorScope(token)}`;
}

function loadCursor(storageKey: string): string | undefined {
  const cursor = localStorage.getItem(storageKey);
  return cursor && cursor.trim() ? cursor : undefined;
}

function saveCursor(storageKey: string, cursor: string): void {
  localStorage.setItem(storageKey, cursor);
}

function isEnvelope(value: unknown): value is InspectionAlertEnvelope {
  if (!value || typeof value !== "object") return false;
  const envelope = value as Partial<InspectionAlertEnvelope>;
  if (!isNonEmptyString(envelope.cursor) || !envelope.alert || typeof envelope.alert !== "object") {
    return false;
  }
  const alert = envelope.alert as Partial<InspectionAlert>;
  return isCanonicalUuid(alert.event_id)
    && isCanonicalUuid(alert.organization_id)
    && isCanonicalUuid(alert.camera_id)
    && isValidOccurredAt(alert.occurred_at)
    && isNonEmptyString(alert.defect_class)
    && typeof alert.confidence === "number"
    && Number.isFinite(alert.confidence)
    && alert.confidence >= 0
    && alert.confidence <= 1;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isCanonicalUuid(value: unknown): value is string {
  return isNonEmptyString(value) && CANONICAL_UUID.test(value);
}

function isValidOccurredAt(value: unknown): value is string {
  if (!isNonEmptyString(value)) return false;
  const parts = ISO_DATETIME_WITH_TIMEZONE.exec(value);
  if (!parts) return false;
  const year = Number(parts[1]);
  const month = Number(parts[2]);
  const day = Number(parts[3]);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return year >= 1 && day >= 1 && day <= daysInMonth[month - 1] && Number.isFinite(Date.parse(value));
}

/**
 * Reconcile durable facts first, then open one ticket-authenticated socket.
 * Cursor strings are deliberately never parsed: their ordering belongs to the server.
 */
export function useInspectionFeed(
  _since: string,
): { alerts: InspectionAlert[]; reconnecting: boolean } {
  const token = localStorage.getItem("odp_token");
  const [alerts, setAlerts] = useState<InspectionAlert[]>([]);
  const [reconnecting, setReconnecting] = useState(false);

  useEffect(() => {
    if (!token) return undefined;

    const cursorStorageKey = inspectionAlertCursorStorageKey(token);
    let cursor = loadCursor(cursorStorageKey);
    const eventIds = new Set<string>();
    let stopped = false;
    let socket: WebSocket | undefined;
    let reconnectTimer: number | undefined;
    let stableTimer: number | undefined;
    let activeRequest: AbortController | undefined;
    let retries = 0;

    setAlerts([]);

    const clearTimers = () => {
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      if (stableTimer !== undefined) window.clearTimeout(stableTimer);
      reconnectTimer = undefined;
      stableTimer = undefined;
    };

    const accept = (candidate: unknown): boolean => {
      if (!isEnvelope(candidate) || eventIds.has(candidate.alert.event_id)) return false;
      eventIds.add(candidate.alert.event_id);
      cursor = candidate.cursor;
      saveCursor(cursorStorageKey, candidate.cursor);
      setAlerts((current) => [...current, candidate.alert]);
      return true;
    };

    const scheduleReconnect = () => {
      if (stopped || reconnectTimer !== undefined) return;
      const delay = RECONNECT_DELAYS_MS[Math.min(retries, RECONNECT_DELAYS_MS.length - 1)];
      retries += 1;
      setReconnecting(true);
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = undefined;
        void connect();
      }, delay);
    };

    const connect = async () => {
      try {
        activeRequest?.abort();
        const request = new AbortController();
        activeRequest = request;
        // REST is the durable fact source. The subsequent socket resumes from
        // the cursor it establishes, covering the interval between both calls.
        const recovered = await reconcileInspectionAlerts(cursor, request.signal);
        if (stopped) return;
        for (const item of Array.isArray(recovered.items) ? recovered.items : []) accept(item);

        const { ticket } = await createWebSocketTicket(request.signal);
        if (stopped) return;
        const query = new URLSearchParams({ ticket });
        if (cursor) query.set("cursor", cursor);
        const connection = new WebSocket(
          `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/inspection-events?${query.toString()}`,
        );
        socket = connection;
        connection.onopen = () => {
          if (stopped || socket !== connection) return;
          stableTimer = window.setTimeout(() => {
            retries = 0;
            stableTimer = undefined;
            setReconnecting(false);
          }, STABLE_CONNECTION_MS);
        };
        connection.onmessage = (event) => {
          if (stopped || socket !== connection) return;
          try {
            accept(JSON.parse(event.data));
          } catch {
            // A malformed transport frame cannot update either feed or cursor.
          }
        };
        connection.onclose = () => {
          if (socket !== connection) return;
          if (stableTimer !== undefined) window.clearTimeout(stableTimer);
          stableTimer = undefined;
          scheduleReconnect();
        };
      } catch {
        // A failed ticket request follows exactly the same bounded backoff and
        // cannot create a socket because construction is below the await.
        scheduleReconnect();
      }
    };

    void connect();
    return () => {
      stopped = true;
      clearTimers();
      activeRequest?.abort();
      socket?.close();
    };
  }, [_since, token]);

  return { alerts, reconnecting };
}
