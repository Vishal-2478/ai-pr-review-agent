"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/$/, "");
const TOKEN_KEY = "pr-review-token";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export function getToken(): string {
  try {
    return window.localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

export function setToken(token: string): void {
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: the token lives for this page only */
  }
  window.dispatchEvent(new Event("pr-review-token"));
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(init?.headers || {}),
      },
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, `Cannot reach the API at ${API_URL}. Is the backend running?`);
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export interface Loaded<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
}

/** Fetch on mount, optionally re-fetch every `refreshMs`, and again when the token changes. */
export function useApi<T>(path: string | null, refreshMs = 0): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const alive = useRef(true);

  const load = useCallback(() => {
    if (!path) return;
    api<T>(path)
      .then((d) => {
        if (!alive.current) return;
        setData(d);
        setError(null);
      })
      .catch((e: ApiError) => alive.current && setError(e))
      .finally(() => alive.current && setLoading(false));
  }, [path]);

  useEffect(() => {
    alive.current = true;
    setLoading(true);
    load();
    const timer = refreshMs > 0 ? window.setInterval(load, refreshMs) : 0;
    window.addEventListener("pr-review-token", load);
    return () => {
      alive.current = false;
      if (timer) window.clearInterval(timer);
      window.removeEventListener("pr-review-token", load);
    };
  }, [load, refreshMs]);

  return { data, error, loading, reload: load };
}

// ---------------------------------------------------------------- API shapes
export interface Review {
  id: string;
  repo: string;
  pr_number: number;
  head_sha: string;
  title: string;
  author: string;
  html_url: string;
  status: string;
  decision: string | null;
  overall_confidence: number | null;
  coverage: number | null;
  reasons: string[];
  cost_usd: number;
  github_review_id?: number | null;
  error?: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface Finding {
  finding_id: string;
  review_id: string;
  agent_type: string;
  severity: "critical" | "high" | "medium" | "low" | "info";
  category: string;
  summary: string;
  file_path: string;
  line_start: number;
  line_end: number;
  suggestion: string;
  confidence: number;
  raw_confidence: number | null;
  rationale: string;
  evidence: string;
  grounded: boolean;
  in_diff: boolean;
  source: string;
  corroborated_by: string[];
  status: string;
  adjustments: string[];
}

export interface QueueItem {
  id: string;
  review_id: string;
  kind: "approval" | "escalation" | "dispute";
  finding_id: string | null;
  status: string;
  priority: number;
  reason: string;
  requested_by: string;
  created_at: string;
  repo: string;
  pr_number: number;
  title: string;
  author: string;
  html_url: string;
  head_sha: string;
  overall_confidence: number | null;
  pending_findings: number;
}

export interface QueueStats {
  pending: number;
  oldest_pending_s: number;
  pending_escalations: number;
  pending_disputes: number;
  resolved_24h: number;
  reviews_24h: number;
  escalation_rate_24h: number;
}

export interface TraceEvent {
  ts: string;
  agent: string;
  event_type: string;
  name: string | null;
  span_id: string | null;
  parent_span: string | null;
  model: string | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cost_usd: number | null;
  latency_ms: number | null;
  outcome: string | null;
  confidence: number | null;
  payload: Record<string, unknown>;
}

export interface AgentHealth {
  agent: string;
  llm_calls: number;
  cost_usd: number;
  tokens_in: number;
  tokens_out: number;
  p95_latency_ms: number | null;
  errors: number;
  feedback_total: number;
  feedback_negative: number;
}

export interface Economics {
  window_hours: number;
  budget: {
    spent_today: number;
    daily_cap: number;
    per_review_cap: number;
    ratio: number;
    allowed: boolean;
    downgrade: boolean;
    reason: string;
  };
  agents: AgentHealth[];
  timeseries: { t: string; cost_usd: number; llm_calls: number }[];
  top_reviews: {
    review_id: string;
    cost_usd: number;
    tokens_in: number;
    tokens_out: number;
    repo?: string;
    pr_number?: number;
    title?: string;
    status?: string;
  }[];
  advice: { agent: string; kind: string; message: string }[];
  routing: { provider: string; models: Record<string, string>; unpriced: string[] };
}
