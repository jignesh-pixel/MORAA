// Apps Script → this route → WhatsApp. Used for morning lists, reminders and reports.
import crypto from 'node:crypto';
import { NextResponse } from 'next/server';
import { deliverNotifications, OpsNotification } from '@/lib/ops';

export const runtime = 'nodejs';

/** Constant-time compare of the shared secret (no early exit on the first wrong byte). */
function secretMatches(provided: unknown): boolean {
  const expected = process.env.OPS_SECRET;
  if (!expected || typeof provided !== 'string') return false;
  const a = crypto.createHash('sha256').update(provided, 'utf8').digest();
  const b = crypto.createHash('sha256').update(expected, 'utf8').digest();
  return crypto.timingSafeEqual(a, b);
}

export async function POST(req: Request) {
  const body = await req.json().catch(() => null);
  if (!body || !secretMatches(body.secret)) {
    return NextResponse.json({ ok: false, error: 'unauthorized' }, { status: 401 });
  }
  if (process.env.OPS_ENABLED !== 'true') {
    // Apps Script will fall back to email
    return NextResponse.json({ ok: false, error: 'ops_disabled' }, { status: 503 });
  }
  const list: OpsNotification[] = Array.isArray(body.notifications) ? body.notifications : [];
  const results = await deliverNotifications(list); // sendInternal() blocks anyone outside OPS_TEAM
  return NextResponse.json({ ok: true, results });
}
