// Client for the read-only WhatsApp chat dashboard API (backend/app/api/routes/dashboard.py).
// The login token is kept in sessionStorage only: closing the browser tab signs you out.

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const TOKEN_KEY = "moraa-chats-token";

export interface ChatCustomer {
  phone: string;
  name: string | null;
  business: string | null;
  last_at: string | null;
  last_preview: string;
  last_direction: "in" | "out" | null;
  messages: number;
}

export interface ChatMedia {
  kind: "photo" | "output";
  url: string;
  drive_link: string | null;
}

export interface TimelineItem {
  id: string;
  type: "message" | "event";
  at: string | null;
  text: string | null;
  direction?: "in" | "out";
  msg_type?: string;
  ingestion_id?: string | null;
  media?: ChatMedia | null;
  event?: string;
  link?: string | null;
}

export interface CustomerProfile {
  phone: string;
  registered: boolean;
  first_seen: string | null;
  name?: string;
  business?: string;
  gstin?: string;
  gst_verified?: boolean;
  address?: string;
  tier?: string;
  balance?: number;
  customer_since?: string | null;
  payments?: { at: string | null; amount: number; source: string; reference: string | null }[];
  totals?: { recharged: number; spent: number; refunded: number };
  orders: { id: string; at: string | null; status: string; product: string | null; amount: number; images: number; error: string | null }[];
  invoices: { at: string | null; payment_id: string; amount: number; status: string; number: string | null; link: string | null }[];
}

export interface AuditOrder {
  id: string;
  at: string | null;
  phone: string;
  name: string | null;
  business: string | null;
  status: string;
  product: string | null;
  amount: number;
  error: string | null;
  input: { url: string } | null;
  outputs: { url: string; style: string | null; drive_link: string | null }[];
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function setToken(token: string | null) {
  try {
    if (token) window.sessionStorage.setItem(TOKEN_KEY, token);
    else window.sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage blocked: the page still works until it is closed */
  }
}

export function signOut() {
  setToken(null);
}

export function absoluteMediaUrl(path: string): string {
  return path.startsWith("http") ? path : `${API_BASE_URL}${path}`;
}

export class DashboardAuthError extends Error {}

async function request<T>(path: string): Promise<T> {
  const token = getToken();
  const res = await fetch(`${API_BASE_URL}${path}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (res.status === 401 || res.status === 403) {
    if (res.status === 401) setToken(null);
    const detail = await res.json().catch(() => ({}));
    throw new DashboardAuthError(detail?.detail || "Please sign in");
  }
  if (!res.ok) throw new Error(`The server answered ${res.status}`);
  return res.json() as Promise<T>;
}

export async function loginWithPassword(username: string, password: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) throw new Error(res.status === 401 ? "Wrong username or password" : `Sign-in failed (${res.status})`);
  const data = await res.json();
  setToken(data.access_token);
  await request("/api/dashboard/me"); // makes sure this account may use the dashboard
}

export async function loginWithGoogle(idToken: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/api/auth/google`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id_token: idToken }),
  });
  if (!res.ok) throw new Error(res.status === 403 ? "This Google account is not allowed" : `Sign-in failed (${res.status})`);
  const data = await res.json();
  setToken(data.access_token);
  await request("/api/dashboard/me");
}

export async function checkSession(): Promise<boolean> {
  if (!getToken()) return false;
  try {
    await request("/api/dashboard/me");
    return true;
  } catch {
    return false;
  }
}

export const fetchCustomers = (q: string, offset = 0) =>
  request<{ customers: ChatCustomer[] }>(`/api/dashboard/customers?q=${encodeURIComponent(q)}&offset=${offset}&limit=50`).then((r) => r.customers);

export const fetchTimeline = (phone: string, before?: string) =>
  request<{ items: TimelineItem[]; has_more: boolean }>(
    `/api/dashboard/customers/${encodeURIComponent(phone)}/timeline?limit=150${before ? `&before=${encodeURIComponent(before)}` : ""}`,
  );

export const fetchProfile = (phone: string) => request<CustomerProfile>(`/api/dashboard/customers/${encodeURIComponent(phone)}/profile`);

export const fetchAudit = (days: number, status: string) =>
  request<{ orders: AuditOrder[] }>(`/api/dashboard/audit?days=${days}&limit=100${status ? `&status=${encodeURIComponent(status)}` : ""}`).then((r) => r.orders);
