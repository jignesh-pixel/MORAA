/**
 * Studioo Ops · backend side (Next.js, App Router, Node runtime)
 * ------------------------------------------------------------------
 * The ONLY place that knows team phone numbers and holds the WhatsApp
 * token. Apps Script (the Sheet) never sees either.
 *
 * ENV VARS
 *   OPS_ENABLED=true
 *   OPS_TEAM={"harvil":"919699899825","jignesh":"919820666332","anurag":"919323438262"}
 *   OPS_SHEET_URL=https://script.google.com/macros/s/XXXX/exec
 *   OPS_SECRET=<same value as OPS_SECRET in Apps Script>
 *   OPS_TEMPLATE_LANG=en
 *   WHATSAPP_TOKEN=<already used by the customer flow>
 *   WHATSAPP_PHONE_NUMBER_ID=<already used by the customer flow>
 *   META_APP_SECRET=<Meta App → Settings → Basic → App secret>
 *   GRAPH_VERSION=v21.0   (use whatever version the customer flow uses)
 */
import crypto from 'crypto';

const GRAPH = () => `https://graph.facebook.com/${process.env.GRAPH_VERSION || 'v21.0'}`;

// ─────────────── team (default-deny) ───────────────

type TeamKey = 'harvil' | 'jignesh' | 'anurag';

function team(): Record<string, string> {
  try {
    return JSON.parse(process.env.OPS_TEAM || '{}');
  } catch {
    console.error('[ops] OPS_TEAM is not valid JSON');
    return {};
  }
}

const digits = (s: string) => String(s || '').replace(/\D/g, '');

/** Phone ("919699899825" or "+91 96998 99825") → team key, or null. */
export function internalKeyFor(phone: string): TeamKey | null {
  if (process.env.OPS_ENABLED !== 'true') return null;
  const p = digits(phone);
  const hit = Object.entries(team()).find(([, num]) => digits(num) === p);
  return (hit?.[0] as TeamKey) ?? null;
}

// ─────────────── 1. Meta signature check (use for ALL webhooks) ───────────────

/** rawBody must be the exact string Meta sent. Read it with `await req.text()` BEFORE JSON.parse. */
export function verifyMetaSignature(rawBody: string, header: string | null): boolean {
  const secret = process.env.META_APP_SECRET;
  if (!secret || !header?.startsWith('sha256=')) return false;
  const expected = 'sha256=' + crypto.createHmac('sha256', secret).update(rawBody, 'utf8').digest('hex');
  const a = Buffer.from(expected), b = Buffer.from(header);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

// ─────────────── 2. Decide: ops or customer flow ───────────────

const OPS_START =
  /^(?:💸|💡|✅|☑|✔|🐞|🐛|📚|(?:exp|expense|idea|task|done|err|error|kb|help|menu|tasks|undo|search|find|report|reports|fix|act|acting|park|parked|drop|dropped)(?=\s|$)|\/)/iu;

/**
 * Internal number + text → ops.
 * Internal number + image/document WITH an ops caption (e.g. "💸 450 chai") → ops, file attached.
 * Internal number + image WITHOUT ops caption → normal generation flow (so the team can test the product).
 */
export function isOpsMessage(msg: WaMessage): boolean {
  if (msg.type === 'text') return true;
  const caption = (msg.image?.caption || msg.document?.caption || '').trim();
  return (msg.type === 'image' || msg.type === 'document') && OPS_START.test(caption);
}

// ─────────────── 3. Handle an ops message ───────────────

export type WaMessage = {
  id: string;
  from: string;
  type: string;
  text?: { body: string };
  image?: { id: string; caption?: string; mime_type?: string };
  document?: { id: string; caption?: string; filename?: string; mime_type?: string };
};

/**
 * Call AFTER you've returned 200 to Meta (or inside waitUntil/after()).
 * Never throws: every failure ends in a WhatsApp message to the sender.
 */
export async function handleOpsMessage(msg: WaMessage, key: TeamKey): Promise<void> {
  try {
    const text = msg.text?.body ?? msg.image?.caption ?? msg.document?.caption ?? '';
    let attachment_url = '';
    const media = msg.image ?? msg.document;
    if (media?.id) {
      try {
        attachment_url = await storeOpsMedia(media.id, media.mime_type || 'application/octet-stream', msg.id);
      } catch (e) {
        console.error('[ops] media store failed', e);
      }
    }

    const res = await postToSheet({ msg_id: msg.id, from: key, text, attachment_url });
    if (!res) {
      await sendInternal(key, '⚠️ Could not reach the sheet. This was NOT saved. Please send it again in a few minutes.');
      return;
    }
    if (res.reply) await sendInternal(key, res.reply);
    if (Array.isArray(res.notify) && res.notify.length) await deliverNotifications(res.notify);
  } catch (e) {
    console.error('[ops] handleOpsMessage', e);
    await sendInternal(key, '⚠️ Something went wrong. This was NOT saved. Please send it again.').catch(() => {});
  }
}

type SheetResponse = { ok: boolean; reply?: string | null; ref?: string; error?: string; notify?: OpsNotification[] };

/** Up to 3 tries. Safe to retry: the sheet ignores a msg_id it has already saved. */
async function postToSheet(payload: Record<string, string>): Promise<SheetResponse | null> {
  const body = JSON.stringify({ secret: process.env.OPS_SECRET, ...payload });
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const r = await fetch(process.env.OPS_SHEET_URL!, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        redirect: 'follow', // Apps Script answers with a 302; this must be "follow"
        signal: AbortSignal.timeout(25_000),
      });
      const data = (await r.json()) as SheetResponse;
      if (data.ok) return data;
      if (data.error === 'unauthorized' || data.error === 'unknown_sender') {
        console.error('[ops] sheet rejected request:', data.error);
        return null;
      }
      if (data.error === 'internal' && data.reply) return data; // sheet already wrote a helpful reply
    } catch (e) {
      console.warn(`[ops] sheet attempt ${attempt} failed`, e);
    }
    await new Promise(res => setTimeout(res, attempt * 2000));
  }
  return null;
}

// ─────────────── 4. The ONLY way ops content leaves the system ───────────────

export async function sendInternal(key: string, text: string): Promise<boolean> {
  const to = team()[key];
  if (!to) {
    console.error(`[ops] BLOCKED: "${key}" is not an internal member`);
    return false;
  }
  return waSend({ messaging_product: 'whatsapp', to: digits(to), type: 'text', text: { body: text.slice(0, 4096), preview_url: false } });
}

async function sendInternalTemplate(key: string, name: string, params: string[]): Promise<boolean> {
  const to = team()[key];
  if (!to) {
    console.error(`[ops] BLOCKED template to "${key}"`);
    return false;
  }
  return waSend({
    messaging_product: 'whatsapp',
    to: digits(to),
    type: 'template',
    template: {
      name,
      language: { code: process.env.OPS_TEMPLATE_LANG || 'en' },
      components: [{ type: 'body', parameters: params.map(p => ({ type: 'text', text: p || '-' })) }],
    },
  });
}

async function waSend(payload: unknown): Promise<boolean> {
  const r = await fetch(`${GRAPH()}/${process.env.WHATSAPP_PHONE_NUMBER_ID}/messages`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${process.env.WHATSAPP_TOKEN}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!r.ok) console.error('[ops] WhatsApp send failed', r.status, await r.text());
  return r.ok;
}

// ─────────────── 5. Notifications from the sheet ───────────────

export type OpsNotification = {
  to: string;
  text: string;
  subject: string;
  template: { name: string; params: string[] };
  use_template: boolean;
};

/**
 * Inside the 24h window → full text.
 * Outside it → approved template (short), then the full text is available with "report"/"tasks".
 * If the plain text fails (window closed after all), falls back to the template.
 */
export async function deliverNotifications(list: OpsNotification[]): Promise<{ ok: boolean }[]> {
  const results: { ok: boolean }[] = [];
  for (const n of list) {
    let ok = false;
    if (!n.use_template) ok = await sendInternal(n.to, n.text);
    if (!ok) ok = await sendInternalTemplate(n.to, n.template.name, n.template.params);
    results.push({ ok });
  }
  return results;
}

// ─────────────── 6. Receipts & screenshots ───────────────

/**
 * Meta media links expire quickly, so download now and store permanently.
 * Returns a link the team can open from the sheet.
 *
 * Default: Supabase Storage, private bucket "ops-media", 10-year signed URL.
 * Swap the upload block for your own storage if you prefer.
 */
async function storeOpsMedia(mediaId: string, mime: string, msgId: string): Promise<string> {
  const auth = { Authorization: `Bearer ${process.env.WHATSAPP_TOKEN}` };
  const meta = await fetch(`${GRAPH()}/${mediaId}`, { headers: auth }).then(r => r.json());
  const bytes: ArrayBuffer = await fetch(meta.url, { headers: auth }).then(r => r.arrayBuffer());
  const file = Buffer.from(new Uint8Array(bytes));

  const { createClient } = await import('@supabase/supabase-js');
  const sb = createClient(process.env.SUPABASE_URL!, process.env.SUPABASE_SERVICE_ROLE_KEY!);
  const ext = (mime.split('/')[1] || 'bin').split(';')[0];
  const path = `${new Date().toISOString().slice(0, 7)}/${msgId.replace(/[^\w.-]/g, '_')}.${ext}`;
  const up = await sb.storage.from('ops-media').upload(path, file, { contentType: mime, upsert: true });
  if (up.error) throw up.error;
  const signed = await sb.storage.from('ops-media').createSignedUrl(path, 60 * 60 * 24 * 365 * 10);
  if (signed.error) throw signed.error;
  return signed.data.signedUrl;
}
