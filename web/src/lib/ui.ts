/**
 * The shared style vocabulary for the v0.0.5 screens (MOD-11/12/13).
 *
 * The shell deliberately has no CSS framework (RISK-08: keep it minimal), so
 * consistency comes from one place instead of per-page improvisation: a small
 * gray-on-white palette that matches the Layout chrome and SyncBadge, one
 * type scale, and one control shape. Pages compose these objects inline.
 */

import type { CSSProperties } from "react";

export const palette = {
  text: "#111827",
  muted: "#6b7280",
  border: "#e5e7eb",
  surface: "#f9fafb",
  accent: "#1d4ed8",
  danger: "#b91c1c",
  warnBg: "#fff3cd",
  warnText: "#7a5c00",
} as const;

export const table: CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
  fontSize: "0.875rem",
};

export const th: CSSProperties = {
  textAlign: "left",
  padding: "0.5rem 0.6rem",
  borderBottom: `2px solid ${palette.border}`,
  color: palette.muted,
  fontWeight: 600,
  fontSize: "0.75rem",
  textTransform: "uppercase",
  letterSpacing: "0.03em",
};

export const td: CSSProperties = {
  padding: "0.5rem 0.6rem",
  borderBottom: `1px solid ${palette.border}`,
  verticalAlign: "top",
};

export const input: CSSProperties = {
  padding: "0.35rem 0.5rem",
  border: `1px solid ${palette.border}`,
  borderRadius: 6,
  fontSize: "0.875rem",
  fontFamily: "inherit",
};

export const label: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 2,
  fontSize: "0.75rem",
  color: palette.muted,
  fontWeight: 600,
};

export const button: CSSProperties = {
  padding: "0.35rem 0.8rem",
  border: `1px solid ${palette.border}`,
  borderRadius: 6,
  background: "white",
  color: palette.text,
  fontSize: "0.875rem",
  fontWeight: 600,
  cursor: "pointer",
};

export const primaryButton: CSSProperties = {
  ...button,
  background: palette.accent,
  borderColor: palette.accent,
  color: "white",
};

export const dangerButton: CSSProperties = {
  ...button,
  color: palette.danger,
  borderColor: "#fecaca",
};

// A button that reads as an inline link (for low-emphasis affordances inside a
// sentence, e.g. "switch back to a scoped agent token"). Keeps the semantics of
// a button (an action) while looking like prose.
export const linkButton: CSSProperties = {
  border: "none",
  background: "none",
  padding: 0,
  color: palette.accent,
  font: "inherit",
  textDecoration: "underline",
  cursor: "pointer",
};

export const card: CSSProperties = {
  border: `1px solid ${palette.border}`,
  borderRadius: 8,
  padding: "1rem",
  background: "white",
};

export const muted: CSSProperties = {
  color: palette.muted,
  fontSize: "0.875rem",
};

export const errorText: CSSProperties = {
  color: palette.danger,
  fontSize: "0.875rem",
};

export const sectionHeading: CSSProperties = {
  fontSize: "1rem",
  margin: "1.5rem 0 0.5rem",
};

export const chip: CSSProperties = {
  display: "inline-block",
  padding: "0.1rem 0.5rem",
  borderRadius: "999px",
  fontSize: "0.75rem",
  fontWeight: 600,
  background: palette.surface,
  border: `1px solid ${palette.border}`,
  color: palette.text,
};
