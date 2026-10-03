"use client";

import { useEffect, useState } from "react";
import { X, ExternalLink, Loader2 } from "lucide-react";
import { fetchProfile, type CustomerProfile } from "@/services/chat-dashboard.service";

const rupees = (n: number) => `₹${n.toLocaleString("en-IN")}`;
const when = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-3 py-1.5 text-sm">
      <span style={{ color: "var(--theme-text-secondary)" }}>{label}</span>
      <span className="text-right font-medium break-words" style={{ color: "var(--theme-text)" }}>{value || "—"}</span>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-5">
      <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide" style={{ color: "var(--theme-text-secondary)" }}>{title}</h3>
      <div className="rounded-lg border px-3 py-1" style={{ borderColor: "var(--theme-border)", background: "var(--theme-surface)" }}>{children}</div>
    </section>
  );
}

/** The customer's profile: everything about them in one place (opens when you click their name or number). */
export default function ProfilePanel({ phone, onClose }: { phone: string; onClose: () => void }) {
  const [profile, setProfile] = useState<CustomerProfile | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchProfile(phone)
      .then((p) => !cancelled && setProfile(p))
      .catch((e) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [phone]);

  return (
    <aside className="flex h-full w-full flex-col overflow-hidden border-l md:w-[360px]" style={{ borderColor: "var(--theme-border)", background: "var(--theme-bg)" }}>
      <div className="flex items-center justify-between border-b px-4 py-3" style={{ borderColor: "var(--theme-border)" }}>
        <h2 className="text-sm font-semibold" style={{ color: "var(--theme-text)" }}>Customer profile</h2>
        <button aria-label="Close profile" onClick={onClose} className="rounded p-1 hover:opacity-70"><X size={18} /></button>
      </div>
      <div className="flex-1 overflow-y-auto px-4 py-4">
        {error && <p className="text-sm text-red-500">{error}</p>}
        {!profile && !error && <Loader2 className="animate-spin" size={18} />}
        {profile && (
          <>
            <Section title="Who">
              <Row label="Name" value={profile.name} />
              <Row label="Business" value={profile.business} />
              <Row label="WhatsApp number" value={`+${profile.phone}`} />
              <Row label="GSTIN (as entered)" value={profile.gstin ? `${profile.gstin}${profile.gst_verified ? " ✓ verified" : ""}` : null} />
              <Row label="Address" value={profile.address} />
              <Row label="Registered" value={profile.registered ? "Yes" : "No"} />
              <Row label="Customer since" value={when(profile.customer_since ?? profile.first_seen)} />
            </Section>
            {profile.balance !== undefined && (
              <Section title="Wallet">
                <Row label="Balance" value={rupees(profile.balance)} />
                <Row label="Recharged (90 days)" value={profile.totals ? rupees(profile.totals.recharged) : null} />
                <Row label="Spent on orders" value={profile.totals ? rupees(profile.totals.spent) : null} />
                <Row label="Refunded" value={profile.totals ? rupees(profile.totals.refunded) : null} />
              </Section>
            )}
            <Section title={`Payments (${profile.payments?.length ?? 0})`}>
              {(profile.payments ?? []).length === 0 && <p className="py-2 text-sm opacity-60">No payments in the last 90 days.</p>}
              {(profile.payments ?? []).map((p, i) => (
                <div key={i} className="border-b py-2 text-sm last:border-b-0" style={{ borderColor: "var(--theme-border-light)" }}>
                  <div className="flex justify-between font-medium" style={{ color: "var(--theme-text)" }}>
                    <span>{rupees(p.amount)}</span>
                    <span className="text-xs opacity-70">{p.source}</span>
                  </div>
                  <div className="text-xs opacity-70">{when(p.at)}{p.reference ? ` · ${p.reference}` : ""}</div>
                </div>
              ))}
            </Section>
            <Section title={`Invoices (${profile.invoices.length})`}>
              {profile.invoices.length === 0 && <p className="py-2 text-sm opacity-60">No invoices yet.</p>}
              {profile.invoices.map((inv) => (
                <div key={inv.payment_id} className="flex items-center justify-between border-b py-2 text-sm last:border-b-0" style={{ borderColor: "var(--theme-border-light)" }}>
                  <div>
                    <div className="font-medium" style={{ color: "var(--theme-text)" }}>{inv.number ?? "Receipt"} · {rupees(inv.amount)}</div>
                    <div className="text-xs opacity-70">{when(inv.at)} · {inv.status}</div>
                  </div>
                  {inv.link && (
                    <a href={inv.link} target="_blank" rel="noreferrer" className="flex items-center gap-1 text-xs underline">
                      Open in ERPNext <ExternalLink size={12} />
                    </a>
                  )}
                </div>
              ))}
            </Section>
            <Section title={`Orders (${profile.orders.length})`}>
              {profile.orders.length === 0 && <p className="py-2 text-sm opacity-60">No orders in the last 90 days.</p>}
              {profile.orders.map((o) => (
                <div key={o.id} className="border-b py-2 text-sm last:border-b-0" style={{ borderColor: "var(--theme-border-light)" }}>
                  <div className="flex justify-between font-medium" style={{ color: "var(--theme-text)" }}>
                    <span>{o.product === "WHITE_BG" ? "Studio Shot" : "Catalog Pack"} · {rupees(o.amount)}</span>
                    <span className="text-xs">{o.status}</span>
                  </div>
                  <div className="text-xs opacity-70">{when(o.at)} · {o.images} image(s) kept</div>
                  {o.error && <div className="text-xs text-red-500">{o.error}</div>}
                </div>
              ))}
            </Section>
          </>
        )}
      </div>
    </aside>
  );
}
