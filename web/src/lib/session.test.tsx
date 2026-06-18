/** E18-T3 — session wiring: storage, the hook, and the Settings connect flow. */

import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import Settings from "../routes/Settings";
import {
  clearToken,
  getToken,
  looksLikeRuntimeToken,
  runtimeTokenProblem,
  setToken,
  useSession,
} from "./session";

afterEach(() => {
  clearToken();
});

function SessionProbe() {
  const { connected } = useSession();
  return <output>{connected ? "connected" : "disconnected"}</output>;
}

describe("the session store", () => {
  it("persists the token to sessionStorage and trims it", () => {
    setToken("  kq_abc.secret  ");
    expect(getToken()).toBe("kq_abc.secret");
    expect(window.sessionStorage.getItem("kantaq.session.token")).toBe("kq_abc.secret");
  });

  it("ignores an empty token", () => {
    setToken("   ");
    expect(getToken()).toBeNull();
  });

  it("clears storage on clearToken", () => {
    setToken("kq_abc.secret");
    clearToken();
    expect(getToken()).toBeNull();
    expect(window.sessionStorage.getItem("kantaq.session.token")).toBeNull();
  });

  it("drives useSession consumers through connect and disconnect", () => {
    render(<SessionProbe />);
    expect(screen.getByText("disconnected")).toBeTruthy();

    act(() => setToken("kq_abc.secret"));
    expect(screen.getByText("connected")).toBeTruthy();

    act(() => clearToken());
    expect(screen.getByText("disconnected")).toBeTruthy();
  });
});

describe("runtime token validation (DEBT-34)", () => {
  it("accepts a well-shaped kq_ runtime token", () => {
    expect(looksLikeRuntimeToken("kq_01ABC.s3cr3t")).toBe(true);
    expect(runtimeTokenProblem("  kq_01ABC.s3cr3t  ")).toBeNull();
  });

  it("rejects a Supabase key paste by name", () => {
    expect(looksLikeRuntimeToken("eyJhbGciOiJIUzI1NiJ9.payload.sig")).toBe(false);
    expect(runtimeTokenProblem("eyJhbGciOiJIUzI1NiJ9.payload.sig")).toMatch(/Supabase key/);
    expect(runtimeTokenProblem("sb_secret_abc123")).toMatch(/Supabase key/);
  });

  it("rejects empty and malformed values with the token-show hint", () => {
    expect(runtimeTokenProblem("")).toMatch(/kantaq token show/);
    expect(runtimeTokenProblem("kq_nodot")).toMatch(/start with `kq_`/);
    expect(looksLikeRuntimeToken("kq_.secret")).toBe(false); // empty token_id
    expect(looksLikeRuntimeToken("kq_id.")).toBe(false); // empty secret
  });
});

describe("the Settings session panel", () => {
  it("connects from the token form and disconnects again", () => {
    // Settings links to its subpages (E21), so it renders under a router.
    render(
      <MemoryRouter>
        <Settings />
      </MemoryRouter>,
    );
    expect(screen.getByRole("status").textContent).toContain("Not connected");

    fireEvent.change(screen.getByLabelText(/runtime token/i), {
      target: { value: "kq_abc.secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(getToken()).toBe("kq_abc.secret");
    expect(screen.getByRole("status").textContent).toContain("Connected");

    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    expect(getToken()).toBeNull();
    expect(screen.getByRole("status").textContent).toContain("Not connected");
  });

  it("rejects a wrong paste (a Supabase key) instead of silently 401ing", () => {
    render(
      <MemoryRouter>
        <Settings />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByLabelText(/runtime token/i), {
      target: { value: "eyJhbGciOiJIUzI1NiJ9.payload.sig" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(getToken()).toBeNull(); // not stored
    expect(screen.getByRole("alert").textContent).toMatch(/Supabase key/);
    // It also surfaces how to get the right one.
    expect(screen.getAllByText("kantaq token show").length).toBeGreaterThan(0);
  });
});
