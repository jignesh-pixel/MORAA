// Password gate for the dashboard and its server routes.
//
// Off by default: with DASHBOARD_PASSWORD unset nothing changes, so local
// development keeps working. Set DASHBOARD_PASSWORD (and optionally
// DASHBOARD_USER, default "admin") before the dashboard is reachable from
// anywhere but this machine. The browser then shows its Basic-auth prompt for
// every page and for /api/gemini/analyze, which spends the server's Gemini key.
//
// /api/ops/* is excluded: those routes are called by Meta and Apps Script and
// carry their own secret / signature checks.
import { createHash, timingSafeEqual } from 'node:crypto';
import { NextRequest, NextResponse } from 'next/server';

const DEFAULT_USER = 'admin';

function digest(value: string): Buffer {
  return createHash('sha256').update(value, 'utf8').digest();
}

/** Constant-time string comparison; hashing first hides the length too. */
function safeEqual(a: string, b: string): boolean {
  return timingSafeEqual(digest(a), digest(b));
}

function parseBasic(header: string | null): { user: string; password: string } | null {
  if (!header || !header.toLowerCase().startsWith('basic ')) return null;
  let decoded: string;
  try {
    decoded = Buffer.from(header.slice(6).trim(), 'base64').toString('utf8');
  } catch {
    return null;
  }
  const split = decoded.indexOf(':');
  if (split < 0) return null;
  return { user: decoded.slice(0, split), password: decoded.slice(split + 1) };
}

function challenge(): NextResponse {
  return new NextResponse('Authentication required', {
    status: 401,
    headers: { 'WWW-Authenticate': 'Basic realm="MORAA GemVision", charset="UTF-8"' },
  });
}

export function proxy(request: NextRequest): NextResponse {
  const password = process.env.DASHBOARD_PASSWORD;
  if (!password) return NextResponse.next();

  const credentials = parseBasic(request.headers.get('authorization'));
  if (!credentials) return challenge();

  const userOk = safeEqual(credentials.user, process.env.DASHBOARD_USER || DEFAULT_USER);
  const passwordOk = safeEqual(credentials.password, password);
  // Evaluate both before deciding, so timing does not reveal which part failed.
  return userOk && passwordOk ? NextResponse.next() : challenge();
}

export const config = {
  // Everything except the ops routes and Next's own static assets.
  matcher: ['/((?!api/ops/|_next/static|_next/image|favicon.ico).*)'],
};
