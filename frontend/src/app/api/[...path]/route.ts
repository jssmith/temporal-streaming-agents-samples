import { NextRequest } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8001";

/**
 * Streaming proxy for the backend API.
 *
 * Next.js rewrites buffer SSE responses, breaking real-time streaming.
 * This Route Handler proxies requests to the FastAPI backend and streams
 * the response body through without buffering.
 */
async function proxyRequest(req: NextRequest) {
  const path = req.nextUrl.pathname; // e.g. /api/sessions/123/run
  const url = `${BACKEND_URL}${path}${req.nextUrl.search}`;

  const headers: Record<string, string> = {
    "Content-Type": req.headers.get("content-type") || "application/json",
  };

  const init: RequestInit = {
    method: req.method,
    headers,
  };

  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = await req.text();
  }

  const upstream = await fetch(url, init);

  // For SSE / streaming responses, pipe through a TransformStream to
  // ensure Next.js dev server flushes each chunk immediately.
  if (upstream.headers.get("content-type")?.includes("text/event-stream")) {
    const { readable, writable } = new TransformStream();
    const writer = writable.getWriter();
    const reader = upstream.body!.getReader();

    (async () => {
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          await writer.write(value);
        }
      } finally {
        await writer.close();
      }
    })();

    return new Response(readable, {
      status: upstream.status,
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
        "X-Accel-Buffering": "no",
      },
    });
  }

  // For non-streaming responses, forward as-is (use arrayBuffer for binary safety)
  const body = await upstream.arrayBuffer();
  return new Response(body, {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("content-type") || "application/json",
    },
  });
}

export const GET = proxyRequest;
export const POST = proxyRequest;
export const PUT = proxyRequest;
export const DELETE = proxyRequest;

