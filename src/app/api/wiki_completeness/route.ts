import { NextRequest, NextResponse } from 'next/server';

const TARGET_SERVER_BASE_URL = process.env.SERVER_BASE_URL || 'http://localhost:8001';

// Proxy for the TSD/BRD completeness report. Passes format=json|md straight
// through and preserves the upstream content type so Markdown stays text.
export async function GET(request: NextRequest) {
  try {
    const qs = request.nextUrl.searchParams.toString();
    const backendResponse = await fetch(`${TARGET_SERVER_BASE_URL}/wiki_completeness?${qs}`);
    const contentType = backendResponse.headers.get('content-type') || 'application/json';
    const body = await backendResponse.text();
    return new NextResponse(body, {
      status: backendResponse.status,
      headers: { 'Content-Type': contentType },
    });
  } catch (error) {
    console.error('Error fetching completeness report:', error);
    // Non-fatal: return JSON null so the client treats it as "no report".
    return new NextResponse('null', {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}
