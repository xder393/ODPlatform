import { useEffect, useState } from "react";

import { apiFetch } from "../../api/client";

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
    // 无 token 时不建立连接：WS 与补偿 REST 在生产环境都要求 Bearer 鉴权。
    const token = localStorage.getItem("odp_token");
    if (!token) return;

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
      const connection = new WebSocket(
        // 浏览器 WebSocket 无法设置请求头，鉴权走 query param（后端同时支持 Authorization 头）。
        `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/inspection-events?token=${encodeURIComponent(token)}`,
      );
      socket = connection;
      const queued: InspectionAlert[] = [];
      let reconciled = false;

      connection.onopen = async () => {
        try {
          // 补偿请求复用 apiFetch：自动附加 Authorization: Bearer 头。
          const response = await apiFetch(
            `/api/v1/inspection-events?updated_after=${encodeURIComponent(since)}`,
          );
          const recovered = await response.json() as InspectionAlert[];
          if (stopped || socket !== connection) return;
          append(recovered);
          reconciled = true;
          append(queued);
          setReconnecting(false);
        } catch {
          if (!stopped && socket === connection) {
            setReconnecting(true);
            connection.close();
          }
        }
      };
      connection.onmessage = (event) => {
        if (stopped || socket !== connection) return;
        const alert = JSON.parse(event.data) as InspectionAlert;
        if (reconciled) append([alert]);
        else queued.push(alert);
      };
      connection.onclose = () => {
        if (!stopped && socket === connection) {
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
