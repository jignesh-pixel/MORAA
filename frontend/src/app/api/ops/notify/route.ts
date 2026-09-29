// Apps Script → this route → WhatsApp. Used for morning lists, reminders and reports.
import { NextResponse } from 'next/server';
import { deliverNotifications, OpsNotification } from '@/lib/ops';

export const runtime = 'nodejs';

export async function POST(req: Request) {
  const body = await req.json().catch(() => null);
  if (!body || body.secret !== process.env.OPS_SECRET) {
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
