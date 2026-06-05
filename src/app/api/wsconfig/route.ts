import { NextResponse } from 'next/server';

// Exposes runtime websocket connection config to the browser. The API runs on
// a separate port from the web app and the browser connects to it directly
// (Next.js rewrites can't proxy websockets), but the public API port varies
// per deployment (production 8001, staging 8002, ...). Reading it here — at
// request time, from the Next server's environment — lets one image serve any
// port mapping.
export async function GET() {
  return NextResponse.json({
    // Full override for non-same-host setups, e.g. "https://api.example.com"
    wsBaseUrl: process.env.PUBLIC_WS_BASE_URL || null,
    // Same-host port mapping (browser connects to ws://<page-host>:<apiPort>)
    apiPort: process.env.PUBLIC_API_PORT || '8001',
  });
}
