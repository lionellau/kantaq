/**
 * E18-T2/T3 — the typed client against the MockApiServer contract fake:
 * the bearer token rides every request, and a 401 drops the session.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { clearToken, getToken, setToken } from "../lib/session";
import { MockApiServer } from "../test/mockApi";
import { createApiClient } from "./client";

let server: MockApiServer;

beforeEach(() => {
  server = new MockApiServer();
});

afterEach(() => {
  server.restore();
  clearToken();
});

describe("the typed client", () => {
  it("sends the session token as a bearer header", async () => {
    setToken("kq_test.secret");
    server.on("GET /v1/tickets", []);

    const api = createApiClient();
    const { response } = await api.GET("/v1/tickets");

    expect(response.status).toBe(200);
    expect(server.calls[0]?.request.headers.get("authorization")).toBe("Bearer kq_test.secret");
  });

  it("sends no header when there is no session", async () => {
    server.on("GET /v1/tickets", []);

    await createApiClient().GET("/v1/tickets");

    expect(server.calls[0]?.request.headers.get("authorization")).toBeNull();
  });

  it("clears the session when the runtime answers 401", async () => {
    setToken("kq_revoked.token");
    server.on(
      "GET /v1/tickets",
      () => new Response(JSON.stringify({ detail: "invalid or revoked token" }), { status: 401 }),
    );

    const { response } = await createApiClient().GET("/v1/tickets");

    expect(response.status).toBe(401);
    expect(getToken()).toBeNull();
  });

  it("types the tracker payloads end to end", async () => {
    setToken("kq_test.secret");
    server.on("GET /v1/projects", [
      {
        id: "prj_1",
        workspace_id: "ws_1",
        name: "Proj",
        goal: "",
        scope: "",
        owner: null,
        target_date: null,
        status: "active",
        created_at: "2026-01-01T00:00:00",
        updated_at: "2026-01-01T00:00:00",
      },
    ]);

    const { data } = await createApiClient().GET("/v1/projects");

    // `data` is typed from the generated schema — these property accesses are
    // compile-time checked against the OpenAPI document (D-08).
    expect(data?.[0].name).toBe("Proj");
    expect(data?.[0].status).toBe("active");
  });
});

describe("the MockApiServer contract gate", () => {
  it("refuses to mock an endpoint that is not in the OpenAPI document", () => {
    expect(() => server.on("GET /v1/not-a-real-endpoint", [])).toThrow(/not in openapi.json/);
  });

  it("matches templated paths by segment", async () => {
    setToken("kq_test.secret");
    server.on("GET /v1/tickets/{ticket_id}", (request: Request) => ({
      id: new URL(request.url, "http://x").pathname.split("/").at(-1),
    }));

    const { data, response } = await createApiClient().GET("/v1/tickets/{ticket_id}", {
      params: { path: { ticket_id: "tkt_42" } },
    });

    expect(response.status).toBe(200);
    expect(data).toEqual({ id: "tkt_42" });
  });
});
