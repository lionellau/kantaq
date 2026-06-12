/**
 * MockApiServer (MOD-30, lands with E18 per the sprint harness plan).
 *
 * A fetch-level fake of the runtime API for UI component tests. Contract-aware:
 * registering a handler for a path that is not in the checked-in OpenAPI
 * document throws — a test cannot quietly mock an endpoint that does not
 * exist, which is the UI half of the D-08 boundary.
 *
 * Handlers are keyed "METHOD /v1/path/{param}" using the document's own
 * templates; `{param}` matches one segment. Calls are recorded for assertions.
 */

import openapi from "../api/openapi.json";

type JsonBody = unknown;
type Handler = JsonBody | ((request: Request) => JsonBody | Response);

export interface RecordedCall {
  method: string;
  path: string;
  request: Request;
}

const KNOWN_PATHS = Object.keys((openapi as { paths: Record<string, unknown> }).paths);

function assertKnownPath(template: string): void {
  if (!KNOWN_PATHS.includes(template)) {
    const hint = "regenerate with: uv run python -m kantaq_runtime.openapi";
    throw new Error(
      `MockApiServer: "${template}" is not in openapi.json — the contract has no such endpoint (${hint})`,
    );
  }
}

function templateMatches(template: string, pathname: string): boolean {
  const templateParts = template.split("/");
  const pathParts = pathname.split("/");
  if (templateParts.length !== pathParts.length) {
    return false;
  }
  return templateParts.every(
    (part, i) => (part.startsWith("{") && part.endsWith("}")) || part === pathParts[i],
  );
}

export class MockApiServer {
  readonly calls: RecordedCall[] = [];
  private readonly handlers = new Map<string, Handler>();
  private readonly originalFetch: typeof globalThis.fetch;

  constructor() {
    this.originalFetch = globalThis.fetch;
    globalThis.fetch = this.dispatch.bind(this) as typeof globalThis.fetch;
  }

  /** Register a handler; `key` is "METHOD /v1/path/{param}" from the contract. */
  on(key: string, handler: Handler): this {
    const [method, template] = key.split(" ", 2);
    if (!method || !template) {
      throw new Error(`MockApiServer: bad handler key "${key}" (want "GET /v1/...")`);
    }
    assertKnownPath(template);
    this.handlers.set(`${method.toUpperCase()} ${template}`, handler);
    return this;
  }

  /** Restore the real fetch. Call from afterEach. */
  restore(): void {
    globalThis.fetch = this.originalFetch;
  }

  private async dispatch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    const request = new Request(input, init);
    const pathname = new URL(request.url, "http://127.0.0.1:3939").pathname;
    this.calls.push({ method: request.method, path: pathname, request });

    for (const [key, handler] of this.handlers) {
      const [method, template] = key.split(" ", 2);
      if (request.method === method && template && templateMatches(template, pathname)) {
        const body = typeof handler === "function" ? handler(request) : handler;
        if (body instanceof Response) {
          return body;
        }
        return Response.json(body);
      }
    }
    return Response.json(
      { detail: `no handler for ${request.method} ${pathname}` },
      {
        status: 501,
      },
    );
  }
}
