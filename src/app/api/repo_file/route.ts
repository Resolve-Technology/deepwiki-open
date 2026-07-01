import { NextRequest, NextResponse } from 'next/server';

// The target backend server base URL, derived from environment variable or defaulted.
const TARGET_SERVER_BASE_URL = process.env.SERVER_BASE_URL || 'http://localhost:8001';

// Proxy for inline citation source display: forwards the file lookup to the
// backend, which reads from the locally cloned repo. Keeps the (possibly
// LAN-only) git host out of the browser's path.
export async function GET(request: NextRequest) {
  try {
    const qs = request.nextUrl.searchParams.toString();
    const targetUrl = `${TARGET_SERVER_BASE_URL}/repo_file?${qs}`;

    const backendResponse = await fetch(targetUrl, {
      method: 'GET',
      headers: { 'Accept': 'application/json' },
    });

    const body = await backendResponse.json();
    return NextResponse.json(body, { status: backendResponse.status });
  } catch (error) {
    console.error('Error fetching repo file:', error);
    return NextResponse.json({ error: String(error) }, { status: 500 });
  }
}
