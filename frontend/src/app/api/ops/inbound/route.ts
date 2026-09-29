// Python webhook (backend/app/services/ops_forward.py) → this route.
// Receives ONE raw WhatsApp message from a team number, already signature-checked by FastAPI.
// Replies 200 at once; the ops work runs in the background via after().
import crypto from 'crypto';
import { after, NextResponse } from 'next/server';
import { handleOpsMessage, internalKeyFor, isOpsMessage, type WaMessage } from '@/lib/ops';

export const runtime = 'nodejs';

function authorized(header: string | null): boolean {
  const secret = process.env.OPS_SECRET;
  if (!secret || !header) return false;
  const a = Buffer.from(secret), b = Buffer.from(header);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

export async function POST(req: Request) {
  if (!authorized(req.headers.get('x-ops-secret'))) {
    return NextResponse.json({ ok: false, error: 'unauthorized' }, { status: 401 });
  }
  const body = await req.json().catch(() => null);
  const msg = body?.message as WaMessage | undefined;
  if (!msg?.from || !msg?.id) {
    return NextResponse.json({ ok: false, error: 'bad_request' }, { status: 400 });
  }
  // Defense in depth: re-check team membership + ops shape (default-deny; honours OPS_ENABLED).
  const key = internalKeyFor(msg.from);
  if (!key || !isOpsMessage(msg)) {
    return NextResponse.json({ ok: false, error: 'not_ops' }, { status: 202 });
  }
  after(() => handleOpsMessage(msg, key));
  return NextResponse.json({ ok: true });
}
