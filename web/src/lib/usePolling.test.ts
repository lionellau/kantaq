import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePolling } from "./usePolling";

describe("usePolling", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("calls the callback once per interval", () => {
    const fn = vi.fn();
    renderHook(() => usePolling(fn, 2000));
    expect(fn).toHaveBeenCalledTimes(0);
    vi.advanceTimersByTime(2000);
    expect(fn).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(4000);
    expect(fn).toHaveBeenCalledTimes(3);
  });

  it("does not poll when disabled", () => {
    const fn = vi.fn();
    renderHook(() => usePolling(fn, 2000, false));
    vi.advanceTimersByTime(10000);
    expect(fn).toHaveBeenCalledTimes(0);
  });

  it("stops polling after unmount", () => {
    const fn = vi.fn();
    const { unmount } = renderHook(() => usePolling(fn, 2000));
    vi.advanceTimersByTime(2000);
    unmount();
    vi.advanceTimersByTime(6000);
    expect(fn).toHaveBeenCalledTimes(1);
  });
});
