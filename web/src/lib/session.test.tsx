/** E18-T3 — session wiring: storage, the hook, and the Settings connect flow. */

import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import Settings from "../routes/Settings";
import { clearToken, getToken, setToken, useSession } from "./session";

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

describe("the Settings session panel", () => {
  it("connects from the token form and disconnects again", () => {
    render(<Settings />);
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
});
