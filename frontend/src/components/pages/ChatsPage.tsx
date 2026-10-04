"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, FileText, Loader2, LogOut, MessageCircle, Search, ClipboardCheck, ExternalLink } from "lucide-react";
import ImageLightbox from "@/components/chats/ImageLightbox";
import ProfilePanel from "@/components/chats/ProfilePanel";
import {
  absoluteMediaUrl,
  checkSession,
  DashboardAuthError,
  fetchAudit,
  fetchCustomers,
  fetchTimeline,
  loginWithGoogle,
  loginWithPassword,
  safeHref,
  signOut,
  type AuditOrder,
  type ChatCustomer,
  type TimelineItem,
} from "@/services/chat-dashboard.service";

const GOOGLE_CLIENT_ID = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID || "";
const POLL_MS = 20000;

interface Viewing {
  url: string;
  title?: string;
  driveLink?: string | null;
}

const timeOf = (iso: string | null) => (iso ? new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }) : "");
const dayOf = (iso: string | null) =>
  iso ? new Date(iso).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", year: "numeric" }) : "";
const shortAgo = (iso: string | null) => {
  if (!iso) return "";
  const d = new Date(iso);
  const today = new Date();
  return d.toDateString() === today.toDateString() ? timeOf(iso) : d.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
};

// ───────────────────────────── sign in ─────────────────────────────

function SignIn({ onDone }: { onDone: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const googleRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!GOOGLE_CLIENT_ID || !googleRef.current) return;
    const script = document.createElement("script");
    script.src = "https://accounts.google.com/gsi/client";
    script.async = true;
    script.onload = () => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const google = (window as any).google;
      if (!google?.accounts?.id || !googleRef.current) return;
      google.accounts.id.initialize({
        client_id: GOOGLE_CLIENT_ID,
        callback: async (response: { credential: string }) => {
          try {
            await loginWithGoogle(response.credential);
            onDone();
          } catch (e) {
            setError((e as Error).message);
          }
        },
      });
      google.accounts.id.renderButton(googleRef.current, { theme: "outline", size: "large", text: "signin_with" });
    };
    document.body.appendChild(script);
    return () => {
      script.remove();
    };
  }, [onDone]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await loginWithPassword(username.trim(), password);
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto mt-16 w-full max-w-sm rounded-xl border p-6" style={{ borderColor: "var(--theme-border)", background: "var(--theme-surface)" }}>
      <h2 className="mb-1 text-lg font-semibold" style={{ color: "var(--theme-text)" }}>WhatsApp chats</h2>
      <p className="mb-4 text-sm" style={{ color: "var(--theme-text-secondary)" }}>Sign in to see customer chats. Only the owners can open this.</p>
      {GOOGLE_CLIENT_ID && <div ref={googleRef} className="mb-4" />}
      <form onSubmit={submit} className="space-y-3">
        <input
          className="w-full rounded-lg border bg-transparent px-3 py-2 text-sm"
          style={{ borderColor: "var(--theme-border)", color: "var(--theme-text)" }}
          placeholder="Username or email"
          autoComplete="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
        <input
          className="w-full rounded-lg border bg-transparent px-3 py-2 text-sm"
          style={{ borderColor: "var(--theme-border)", color: "var(--theme-text)" }}
          placeholder="Password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {error && <p className="text-sm text-red-500">{error}</p>}
        <button disabled={busy || !username || !password} className="w-full rounded-lg bg-emerald-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50">
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

// ───────────────────────────── conversation ─────────────────────────────

function Bubble({ item, onOpen }: { item: TimelineItem; onOpen: (v: Viewing) => void }) {
  if (item.type === "event") {
    return (
      <div className="my-2 flex justify-center">
        <span className="flex max-w-[80%] items-center gap-2 rounded-full px-3 py-1 text-xs" style={{ background: "#fff3c4", color: "#4a3b00" }}>
          <span>{item.text}</span>
          {safeHref(item.link) && (
            <a href={safeHref(item.link)} target="_blank" rel="noreferrer" className="flex items-center gap-1 underline">
              Open in ERPNext <ExternalLink size={11} />
            </a>
          )}
          <span className="opacity-60">{timeOf(item.at)}</span>
        </span>
      </div>
    );
  }
  const mine = item.direction === "out";
  return (
    <div className={`my-1 flex ${mine ? "justify-end" : "justify-start"}`}>
      <div
        className="max-w-[78%] rounded-lg px-3 py-2 text-sm shadow-sm"
        style={{ background: mine ? "#d9fdd3" : "#ffffff", color: "#111b21" }}
      >
        {item.media && (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={absoluteMediaUrl(item.media.url)}
            alt={item.media.kind === "photo" ? "Photo from the customer" : "Image we sent"}
            loading="lazy"
            className="mb-1 max-h-72 cursor-zoom-in rounded-md object-cover"
            onClick={() => onOpen({ url: item.media!.url, title: item.media!.kind === "photo" ? "Photo from the customer" : "Image we sent", driveLink: item.media!.drive_link })}
          />
        )}
        {item.msg_type === "image" && !item.media && <div className="mb-1 italic opacity-60">📷 Photo (file not kept)</div>}
        {item.msg_type === "document" && <FileText size={14} className="mr-1 inline" />}
        {item.text && <div className="whitespace-pre-wrap break-words">{item.text}</div>}
        <div className="mt-1 text-right text-[10px] opacity-55">{timeOf(item.at)}</div>
      </div>
    </div>
  );
}

function Thread({ phone, onOpenImage }: { phone: string; onOpenImage: (v: Viewing) => void }) {
  const [items, setItems] = useState<TimelineItem[]>([]);
  const [earlier, setEarlier] = useState<TimelineItem[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const earlierLoaded = useRef(false);

  useEffect(() => {
    // The page gives this component a new key per customer, so it always starts from a clean state.
    let cancelled = false;
    let running = false; // a slow answer must not be overtaken (and overwritten) by the next poll
    const run = () => {
      if (running) return;
      running = true;
      fetchTimeline(phone)
        .then((data) => {
          if (cancelled) return;
          setItems(data.items);
          if (!earlierLoaded.current) setHasMore(data.has_more);
          setError(null);
          setLoading(false);
        })
        .catch((e) => {
          if (cancelled) return;
          setError((e as Error).message);
          setLoading(false);
        })
        .finally(() => {
          running = false;
        });
    };
    run();
    const t = setInterval(run, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [phone]);

  const all = useMemo(() => {
    const seen = new Set<string>();
    return [...earlier, ...items]
      .filter((i) => (seen.has(i.id) ? false : (seen.add(i.id), true)))
      .sort((a, b) => (a.at ?? "").localeCompare(b.at ?? ""));
  }, [earlier, items]);

  const loadEarlier = async () => {
    const oldest = all[0]?.at;
    if (!oldest) return;
    setLoadingEarlier(true);
    stickToBottom.current = false;
    try {
      const page = await fetchTimeline(phone, oldest);
      earlierLoaded.current = true;
      setEarlier((prev) => [...page.items, ...prev]);
      setHasMore(page.has_more);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoadingEarlier(false);
    }
  };

  useEffect(() => {
    if (stickToBottom.current) bottomRef.current?.scrollIntoView();
  }, [all]);

  const withDays = useMemo(() => {
    const out: (TimelineItem | { id: string; day: string })[] = [];
    let last = "";
    for (const it of all) {
      const d = dayOf(it.at);
      if (d !== last) {
        out.push({ id: `day-${d}`, day: d });
        last = d;
      }
      out.push(it);
    }
    return out;
  }, [all]);

  if (loading) return <div className="flex flex-1 items-center justify-center"><Loader2 className="animate-spin" /></div>;
  if (error) return <div className="p-6 text-sm text-red-500">{error}</div>;
  if (all.length === 0) return <div className="p-6 text-sm opacity-60">No messages in the last 90 days.</div>;
  return (
    <div
      className="flex-1 overflow-y-auto px-4 py-3"
      style={{ background: "#efeae2" }}
      onScroll={(e) => {
        const el = e.currentTarget;
        stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      }}
    >
      {hasMore && (
        <div className="mb-2 flex justify-center">
          <button
            disabled={loadingEarlier}
            onClick={loadEarlier}
            className="rounded-md bg-white/90 px-3 py-1 text-xs text-slate-700 shadow-sm disabled:opacity-60"
          >
            {loadingEarlier ? "Loading…" : "Load earlier messages"}
          </button>
        </div>
      )}
      {withDays.map((it) =>
        "day" in it ? (
          <div key={it.id} className="my-3 flex justify-center">
            <span className="rounded-md bg-white/90 px-3 py-1 text-xs text-slate-600 shadow-sm">{it.day}</span>
          </div>
        ) : (
          <Bubble key={it.id} item={it} onOpen={onOpenImage} />
        ),
      )}
      <div ref={bottomRef} />
    </div>
  );
}

// ───────────────────────────── weekly audit ─────────────────────────────

function AuditView({ onOpenImage, onOpenChat }: { onOpenImage: (v: Viewing) => void; onOpenChat: (phone: string) => void }) {
  const [days, setDays] = useState(7);
  const [status, setStatus] = useState("");
  const [result, setResult] = useState<{ key: string; orders: AuditOrder[] | null; error: string | null } | null>(null);
  const key = `${days}|${status}`;

  useEffect(() => {
    let cancelled = false;
    fetchAudit(days, status)
      .then((o) => !cancelled && setResult({ key, orders: o, error: null }))
      .catch((e) => !cancelled && setResult({ key, orders: null, error: e.message }));
    return () => {
      cancelled = true;
    };
  }, [days, status, key]);

  const fresh = result && result.key === key ? result : null;
  const orders = fresh?.orders ?? null;
  const error = fresh?.error ?? null;

  const select = "rounded-lg border bg-transparent px-2 py-1 text-sm";
  return (
    <div className="flex-1 overflow-y-auto p-4">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <select className={select} style={{ borderColor: "var(--theme-border)", color: "var(--theme-text)" }} value={days} onChange={(e) => setDays(Number(e.target.value))}>
          <option value={7}>Last 7 days</option>
          <option value={14}>Last 14 days</option>
          <option value={30}>Last 30 days</option>
          <option value={90}>Last 90 days</option>
        </select>
        <select className={select} style={{ borderColor: "var(--theme-border)", color: "var(--theme-text)" }} value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All orders</option>
          <option value="problems">Problems only (failed, rejected, partial)</option>
          <option value="delivered">Delivered</option>
        </select>
        <span className="text-xs opacity-60">What each customer sent, and what they got back.</span>
      </div>
      {error && <p className="text-sm text-red-500">{error}</p>}
      {!orders && !error && <Loader2 className="animate-spin" />}
      {orders && orders.length === 0 && <p className="text-sm opacity-60">No orders in this period.</p>}
      <div className="space-y-3">
        {(orders ?? []).map((o) => (
          <div key={o.id} className="rounded-xl border p-3" style={{ borderColor: "var(--theme-border)", background: "var(--theme-surface)" }}>
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-sm">
              <button className="font-semibold underline-offset-2 hover:underline" style={{ color: "var(--theme-text)" }} onClick={() => onOpenChat(o.phone)}>
                {o.name || `+${o.phone}`}{o.business ? ` · ${o.business}` : ""}
              </button>
              <span className="text-xs opacity-70">
                {dayOf(o.at)} {timeOf(o.at)} · {o.product === "WHITE_BG" ? "Studio Shot" : o.product ? "Catalog Pack" : "No product chosen"} · ₹{o.amount} ·{" "}
                <b className={["failed", "delivery_failed", "rejected"].includes(o.status) ? "text-red-500" : ""}>{o.status}</b>
              </span>
            </div>
            {o.error && <p className="mb-2 text-xs text-red-500">{o.error}</p>}
            <div className="flex flex-wrap items-start gap-2">
              {o.input && (
                <figure className="text-center">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={absoluteMediaUrl(o.input.url)} alt="Customer photo" loading="lazy" className="h-28 w-28 cursor-zoom-in rounded-md object-cover" onClick={() => onOpenImage({ url: o.input!.url, title: "Photo from the customer" })} />
                  <figcaption className="mt-1 text-[11px] opacity-60">Sent</figcaption>
                </figure>
              )}
              <ChevronRight className="mt-10 opacity-40" size={18} />
              {o.outputs.length === 0 && <span className="mt-10 text-xs opacity-60">No image kept</span>}
              {o.outputs.map((x, i) => (
                <figure key={i} className="text-center">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={absoluteMediaUrl(x.url)} alt={x.style ?? "Result"} loading="lazy" className="h-28 w-28 cursor-zoom-in rounded-md object-cover" onClick={() => onOpenImage({ url: x.url, title: x.style ?? "Result", driveLink: x.drive_link })} />
                  <figcaption className="mt-1 max-w-28 truncate text-[11px] opacity-60">{x.style ?? "Received"}</figcaption>
                </figure>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ───────────────────────────── page ─────────────────────────────

export default function ChatsPage() {
  const [signedIn, setSignedIn] = useState<boolean | null>(null);
  const [tab, setTab] = useState<"chats" | "audit">("chats");
  const [query, setQuery] = useState("");
  const [customers, setCustomers] = useState<ChatCustomer[]>([]);
  const [listError, setListError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [showProfile, setShowProfile] = useState(false);
  const [viewing, setViewing] = useState<Viewing | null>(null);

  useEffect(() => {
    checkSession().then(setSignedIn);
  }, []);

  const listSeq = useRef(0);
  const loadList = useCallback(async () => {
    const mine = ++listSeq.current; // only the newest request may update the list
    try {
      const rows = await fetchCustomers(query);
      if (mine !== listSeq.current) return;
      setCustomers(rows);
      setListError(null);
    } catch (e) {
      if (mine !== listSeq.current) return;
      if (e instanceof DashboardAuthError) setSignedIn(false);
      setListError((e as Error).message);
    }
  }, [query]);

  useEffect(() => {
    if (!signedIn) return;
    const t = setTimeout(loadList, 250);
    const poll = setInterval(loadList, POLL_MS);
    return () => {
      clearTimeout(t);
      clearInterval(poll);
    };
  }, [signedIn, loadList]);

  const current = customers.find((c) => c.phone === selected);

  if (signedIn === null) return <div className="flex h-64 items-center justify-center"><Loader2 className="animate-spin" /></div>;
  if (!signedIn) return <SignIn onDone={() => setSignedIn(true)} />;

  return (
    <div className="flex flex-col" style={{ height: "calc(100vh - 140px)", minHeight: 480 }}>
      <div className="mb-3 flex items-center justify-between">
        <div className="flex gap-1 rounded-lg p-1" style={{ background: "var(--theme-muted)" }}>
          {([["chats", "Chats", MessageCircle], ["audit", "Weekly audit", ClipboardCheck]] as const).map(([id, label, Icon]) => (
            <button
              key={id}
              onClick={() => setTab(id)}
              className="flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium"
              style={{ background: tab === id ? "var(--theme-surface)" : "transparent", color: "var(--theme-text)" }}
            >
              <Icon size={15} /> {label}
            </button>
          ))}
        </div>
        <button
          className="flex items-center gap-1 text-xs opacity-70 hover:opacity-100"
          onClick={() => {
            signOut();
            setSignedIn(false);
          }}
        >
          <LogOut size={14} /> Sign out
        </button>
      </div>

      <div className="flex flex-1 overflow-hidden rounded-xl border" style={{ borderColor: "var(--theme-border)", background: "var(--theme-bg)" }}>
        {tab === "audit" ? (
          <AuditView
            onOpenImage={setViewing}
            onOpenChat={(phone) => {
              setSelected(phone);
              setShowProfile(false);
              setTab("chats");
            }}
          />
        ) : (
          <>
            <div className={`${selected ? "hidden md:flex" : "flex"} w-full flex-col border-r md:w-[340px]`} style={{ borderColor: "var(--theme-border)" }}>
              <div className="border-b p-3" style={{ borderColor: "var(--theme-border)" }}>
                <div className="relative">
                  <Search size={15} className="absolute left-3 top-2.5 opacity-50" />
                  <input
                    className="w-full rounded-lg border bg-transparent py-2 pl-9 pr-3 text-sm"
                    style={{ borderColor: "var(--theme-border)", color: "var(--theme-text)" }}
                    placeholder="Search name, business or number"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                  />
                </div>
              </div>
              <div className="flex-1 overflow-y-auto">
                {listError && <p className="p-3 text-sm text-red-500">{listError}</p>}
                {customers.length === 0 && !listError && <p className="p-4 text-sm opacity-60">No chats yet. Chats appear here as customers write in.</p>}
                {customers.map((c) => (
                  <button
                    key={c.phone}
                    onClick={() => {
                      setSelected(c.phone);
                      setShowProfile(false);
                    }}
                    className="flex w-full items-center gap-3 border-b px-3 py-3 text-left hover:opacity-80"
                    style={{ borderColor: "var(--theme-border-light)", background: selected === c.phone ? "var(--theme-muted)" : "transparent" }}
                  >
                    <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-emerald-600 text-sm font-semibold text-white">
                      {(c.name || c.phone).slice(0, 1).toUpperCase()}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex justify-between gap-2">
                        <span className="truncate text-sm font-semibold" style={{ color: "var(--theme-text)" }}>{c.name || `+${c.phone}`}</span>
                        <span className="shrink-0 text-[11px] opacity-60">{shortAgo(c.last_at)}</span>
                      </div>
                      <div className="truncate text-xs opacity-65">
                        {c.business ? `${c.business} · ` : ""}
                        {c.last_direction === "out" ? "You: " : ""}
                        {c.last_preview}
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            </div>

            <div className={`${selected ? "flex" : "hidden md:flex"} min-w-0 flex-1`}>
              {!selected ? (
                <div className="flex flex-1 items-center justify-center text-sm opacity-60">Pick a customer to see their chat.</div>
              ) : (
                <>
                  <div className={`${showProfile ? "hidden lg:flex" : "flex"} min-w-0 flex-1 flex-col`}>
                    <div className="flex items-center gap-3 border-b px-4 py-3" style={{ borderColor: "var(--theme-border)" }}>
                      <button className="text-xs underline md:hidden" onClick={() => setSelected(null)}>Back</button>
                      <button className="flex items-center gap-3 text-left" onClick={() => setShowProfile((v) => !v)} title="Open the profile">
                        <div className="flex h-9 w-9 items-center justify-center rounded-full bg-emerald-600 text-sm font-semibold text-white">
                          {(current?.name || selected).slice(0, 1).toUpperCase()}
                        </div>
                        <div>
                          <div className="text-sm font-semibold" style={{ color: "var(--theme-text)" }}>{current?.name || `+${selected}`}</div>
                          <div className="text-xs opacity-60">{current?.business ? `${current.business} · ` : ""}+{selected} · click for profile</div>
                        </div>
                      </button>
                    </div>
                    <Thread key={selected} phone={selected} onOpenImage={setViewing} />
                  </div>
                  {showProfile && <ProfilePanel key={selected} phone={selected} onClose={() => setShowProfile(false)} />}
                </>
              )}
            </div>
          </>
        )}
      </div>
      {viewing && <ImageLightbox url={viewing.url} title={viewing.title} driveLink={viewing.driveLink} onClose={() => setViewing(null)} />}
    </div>
  );
}
