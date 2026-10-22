const DASHES = /\s*[\u2014\u2013]\s*/g;

export function clean(text: string | null | undefined): string {
  return (text ?? "").replace(DASHES, ": ");
}

export function fmtPct(v: number | null | undefined, digits = 0): string {
  return v == null || Number.isNaN(v) ? "n/a" : `${(v * 100).toFixed(digits)} %`;
}

export function fmtMs(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "n/a";
  return v >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)} s` : `${Math.round(v)} ms`;
}

export function fmtS(v: number | null | undefined, digits = 0): string {
  if (v == null || Number.isNaN(v)) return "n/a";
  return v >= 120 ? `${Math.floor(v / 60)} min ${Math.round(v % 60)} s` : `${v.toFixed(digits)} s`;
}

export function pad(n: number, width = 4): string {
  return String(Math.max(0, Math.floor(n))).padStart(width, "0");
}

export function timecode(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return "t+0.0 s";
  return `t+${seconds.toFixed(1)} s`;
}

export function clock(ts: number | string | null | undefined): string {
  if (ts == null) return "";
  const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
  if (Number.isNaN(d.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(d);
}

export function relTime(ts: number | string | null | undefined, now: number): string {
  if (ts == null) return "";
  const t = typeof ts === "number" ? ts : Date.parse(ts) / 1000;
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, now - t);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export const FAILURE_LABEL: Record<string, string> = {
  container_crash: "Container crash",
  container_stop: "Container stop",
  container_pause: "Process freeze",
  network_delay: "Network latency",
  network_loss: "Packet loss",
  network_partition: "Network partition",
  cpu_stress: "CPU stress",
  disk_pressure: "Disk pressure",
  db_flood: "Database flood",
  traffic_spike: "Traffic spike",
};

export function failureLabel(key: string | null | undefined): string {
  return key ? FAILURE_LABEL[key] ?? key.replaceAll("_", " ") : "";
}

export const CATEGORY_LABEL: Record<string, string> = {
  shallow_health_check: "Shallow health check",
  missing_health_check: "Missing health check",
  no_redundancy: "Single instance",
  no_resource_limits: "No resource limits",
  weak_restart_policy: "Weak restart policy",
  no_retry_or_backoff: "No retry or backoff",
  port_beyond_ingress: "Port beyond ingress",
  no_rate_limiting: "No rate limiting",
};

export function categoryLabel(key: string): string {
  return CATEGORY_LABEL[key] ?? key.replaceAll("_", " ");
}

export function grade(score: number | null | undefined): string {
  if (score == null) return "untested";
  if (score >= 90) return "A";
  if (score >= 80) return "B";
  if (score >= 65) return "C";
  if (score >= 50) return "D";
  return "F";
}
