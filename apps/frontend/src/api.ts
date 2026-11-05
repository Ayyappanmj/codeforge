export type Status = "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "TIMED_OUT" | "CANCELLED";
export const TERMINAL: Status[] = ["SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"];

export interface Summary {
  execution_id: string;
  language: string;
  status: Status;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  execution_duration_ms: number | null;
  exit_code: number | null;
  error_type: string | null;
}
export interface Detail extends Summary {
  stdout: string;
  stderr: string;
  output_truncated: boolean;
  source_code: string;
  stdin: string;
}
export interface Page {
  items: Summary[];
  total: number;
  page: number;
  page_size: number;
}

const BASE = import.meta.env.VITE_API_BASE ?? "/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      const details = body?.error?.details as { message: string }[] | undefined;
      message = details?.length ? details.map((d) => d.message).join("; ") : body?.error?.message ?? message;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(message);
  }
  return res.json() as Promise<T>;
}

export const api = {
  languages: () => request<{ languages: string[] }>("/languages"),
  submit: (language: string, source_code: string, stdin: string) =>
    request<{ execution_id: string; status: Status }>("/executions", {
      method: "POST",
      body: JSON.stringify({ language, source_code, stdin }),
    }),
  get: (id: string) => request<Detail>(`/executions/${id}`),
  cancel: (id: string) => request<{ status: Status }>(`/executions/${id}/cancel`, { method: "POST" }),
  list: (page: number, status: string, language: string) => {
    const q = new URLSearchParams({ page: String(page), page_size: "10" });
    if (status) q.set("status", status);
    if (language) q.set("language", language);
    return request<Page>(`/executions?${q}`);
  },
};
