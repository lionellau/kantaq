/**
 * MOD-11/12 — the sidebar light/dark switch.
 *
 * A single honest control: it shows the mode currently in effect and switches to
 * the other. The label says what's active ("Light"/"Dark"); the aria-label says
 * the action ("Switch to dark theme") so screen-reader users hear the outcome.
 * The half-circle glyph is decorative (aria-hidden) and depicts the duality —
 * no icon library in this framework-free shell (RISK-08).
 */

import { useTheme } from "../lib/theme";
import * as ui from "../lib/ui";

export default function ThemeToggle() {
  const { theme, toggle } = useTheme();
  const isDark = theme === "dark";
  const action = isDark ? "Switch to light theme" : "Switch to dark theme";
  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={action}
      title={action}
      data-theme-state={theme}
      style={{
        ...ui.button,
        width: "100%",
        display: "flex",
        alignItems: "center",
        gap: "var(--space-2)",
        color: ui.palette.muted,
      }}
    >
      <span aria-hidden="true" style={{ fontSize: ui.text.base, lineHeight: 1 }}>
        ◐
      </span>
      <span>{isDark ? "Dark" : "Light"}</span>
    </button>
  );
}
