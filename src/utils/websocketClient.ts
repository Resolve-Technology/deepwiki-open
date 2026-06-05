/**
 * WebSocket client for chat completions
 * This replaces the HTTP streaming endpoint with a WebSocket connection
 */

// Get the server base URL from environment or use default
const SERVER_BASE_URL = process.env.SERVER_BASE_URL || 'http://localhost:8001';

// The browser connects to the API directly (the Next.js rewrites can't proxy
// websockets), and the API's public port varies per deployment (e.g. 8001 in
// production, 8002 on a staging container). That port is runtime config on the
// Next server, so fetch it once and cache it. Until the fetch resolves we fall
// back to the page's own host on port 8001 — the compose default.
let runtimeWsBase: string | null = null;

if (typeof window !== 'undefined') {
  fetch('/api/wsconfig')
    .then(res => (res.ok ? res.json() : null))
    .then(config => {
      if (config?.wsBaseUrl) {
        runtimeWsBase = config.wsBaseUrl.replace(/^http/, 'ws');
      } else if (config?.apiPort) {
        const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
        runtimeWsBase = `${proto}://${window.location.hostname}:${config.apiPort}`;
      }
    })
    .catch(() => { /* keep the same-host fallback */ });
}

// Resolve the websocket URL for the chat endpoint
export const getWebSocketUrl = () => {
  if (runtimeWsBase) {
    return `${runtimeWsBase}/ws/chat`;
  }
  if (typeof window !== 'undefined') {
    // Same host as the page, default API port
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    return `${proto}://${window.location.hostname}:8001/ws/chat`;
  }
  // Server-side: replace http:// with ws:// or https:// with wss://
  return `${SERVER_BASE_URL.replace(/^http/, 'ws')}/ws/chat`;
};

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

export interface ChatCompletionRequest {
  repo_url: string;
  messages: ChatMessage[];
  filePath?: string;
  token?: string;
  type?: string;
  provider?: string;
  model?: string;
  rag_query?: string;
  language?: string;
  excluded_dirs?: string;
  excluded_files?: string;
}

/**
 * Creates a WebSocket connection for chat completions
 * @param request The chat completion request
 * @param onMessage Callback for received messages
 * @param onError Callback for errors
 * @param onClose Callback for when the connection closes
 * @returns The WebSocket connection
 */
export const createChatWebSocket = (
  request: ChatCompletionRequest,
  onMessage: (message: string) => void,
  onError: (error: Event) => void,
  onClose: () => void
): WebSocket => {
  // Create WebSocket connection
  const ws = new WebSocket(getWebSocketUrl());
  
  // Set up event handlers
  ws.onopen = () => {
    console.log('WebSocket connection established');
    // Send the request as JSON
    ws.send(JSON.stringify(request));
  };
  
  ws.onmessage = (event) => {
    // Call the message handler with the received text
    onMessage(event.data);
  };
  
  ws.onerror = (error) => {
    console.error('WebSocket error:', error);
    onError(error);
  };
  
  ws.onclose = () => {
    console.log('WebSocket connection closed');
    onClose();
  };
  
  return ws;
};

/**
 * Closes a WebSocket connection
 * @param ws The WebSocket connection to close
 */
export const closeWebSocket = (ws: WebSocket | null): void => {
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
    ws.close();
  }
};
