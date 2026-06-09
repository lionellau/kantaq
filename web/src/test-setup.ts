import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Testing Library does not auto-clean between tests when Vitest globals are off,
// so unmount the rendered tree after each test (otherwise DOM accumulates and
// landmark/heading queries match multiple nodes).
afterEach(() => {
  cleanup();
});
