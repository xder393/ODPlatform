import { useEffect, useState } from "react";

export interface InspectionAlert {
  event_id: string;
  organization_id: string;
  camera_id: string;
  occurred_at: string;
  defect_class: string;
  confidence: number;
}

export function useInspectionFeed(
  since: string,
): { alerts: InspectionAlert[]; reconnecting: boolean } {
  const [alerts, setAlerts] = useState<InspectionAlert[]>([]);
  const [reconnecting, setReconnecting] = useState(false);

  useEffect(() => {
    let stopped = false;
    let socket: WebSocket | undefined;
    let reconnectTimer: number | undefined;

    const append = (incoming: InspectionAlert[]) => {
      setAlerts((current) => {
        const known = new Set(current.map((alert) => alert.event_id));
        return [...current, ...incoming.filter((alert) => !known.has(alert.event_id))];
      });
    };

    const connect = () => {
      socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/inspection-events`);
      const queued: InspectionAlert[] = [];
      let reconciled = false;

      socket.onopen = async () => {
        try {
          const response = await fetch(`/api/v1/inspection-events?updated_after=${encodeURIComponent(since)}`);
          if (!response.ok) throw new Error("Inspection event reconciliation failed");
          append(await response.json() as InspectionAlert[]);
          reconciled = true;
          append(queued);
          setReconnecting(false);
        } catch {
          setReconnecting(true);
        }
      };
      socket.onmessage = (event) => {
        const alert = JSON.parse(event.data) as InspectionAlert;
        if (reconciled) append([alert]);
        else queued.push(alert);
      };
      socket.onclose = () => {
        if (!stopped) {
          setReconnecting(true);
          reconnectTimer = window.setTimeout(connect, 1000);
        }
      };
    };

    connect();
    return () => {
      stopped = true;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [since]);

  return { alerts, reconnecting };
}
