import { render } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { routes } from "../router";

/**
 * Shared Vitest render helper (MOD-30 web harness). Renders the full app at a
 * given route so component tests do not re-wire the router each time. A
 * MockApiServer (generated from the FastAPI OpenAPI) lands with the typed client
 * in E18-T2 (Sprint 2).
 */
export function renderApp(initialPath = "/") {
  const router = createMemoryRouter(routes, { initialEntries: [initialPath] });
  return render(<RouterProvider router={router} />);
}
