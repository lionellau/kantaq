/**
 * The shared style vocabulary for the app shell (MOD-11/12/13).
 *
 * The shell deliberately has no CSS framework (RISK-08): consistency comes from
 * ONE token source (`src/index.css` CSS variables) referenced here. No value is
 * hardcoded — every color, radius, shadow, and font reads a `var(--…)`, so the
 * whole UI re-themes (light/dark) from `index.css` alone. Pages compose these
 * objects inline; status surfaces use the `status` tokens / `statusChip` helper
 * instead of per-component hex.
 */

import type { CSSProperties } from "react";

export const palette = {
  bg: "var(--color-bg)",
  raised: "var(--color-raised)",
  surface: "var(--color-surface)",
  text: "var(--color-text)",
  muted: "var(--color-text-muted)",
  subtle: "var(--color-text-subtle)",
  border: "var(--color-border)",
  borderStrong: "var(--color-border-strong)",
  accent: "var(--color-accent)",
  accentHover: "var(--color-accent-hover)",
  accentSoft: "var(--color-accent-soft)",
  onAccent: "var(--color-on-accent)",
  danger: "var(--color-danger-text)",
  dangerBorder: "var(--color-danger-border)",
  warnBg: "var(--color-warning-bg)",
  warnText: "var(--color-warning-text)",
} as const;

export const radius = {
  sm: "var(--radius-sm)",
  md: "var(--radius-md)",
  pill: "var(--radius-pill)",
} as const;

export const shadow = {
  sm: "var(--shadow-sm)",
  md: "var(--shadow-md)",
} as const;

export const font = {
  sans: "var(--font-sans)",
  mono: "var(--font-mono)",
} as const;

export const text = {
  xs: "var(--text-xs)",
  sm: "var(--text-sm)",
  base: "var(--text-base)",
  lg: "var(--text-lg)",
  xl: "var(--text-xl)",
} as const;

/**
 * Semantic status tokens — one success/warning/danger/neutral set, used by every
 * badge/card so a "committed" green is the SAME green everywhere (no more
 * green-vs-green drift across components) and re-themes in dark mode.
 */
export type StatusKind = "success" | "warning" | "danger" | "neutral";

export const status: Record<StatusKind, { bg: string; text: string }> = {
  success: { bg: "var(--color-success-bg)", text: "var(--color-success-text)" },
  warning: { bg: "var(--color-warning-bg)", text: "var(--color-warning-text)" },
  danger: { bg: "var(--color-danger-bg)", text: "var(--color-danger-text)" },
  neutral: { bg: "var(--color-neutral-bg)", text: "var(--color-neutral-text)" },
};

export const table: CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
  fontSize: text.sm,
};

export const th: CSSProperties = {
  textAlign: "left",
  padding: "var(--space-2) var(--space-3)",
  borderBottom: `1px solid ${palette.borderStrong}`,
  color: palette.muted,
  fontWeight: 600,
  fontSize: text.xs,
  textTransform: "uppercase",
  letterSpacing: "0.04em",
};

export const td: CSSProperties = {
  padding: "var(--space-2) var(--space-3)",
  borderBottom: `1px solid ${palette.border}`,
  verticalAlign: "top",
};

export const input: CSSProperties = {
  padding: "0.4rem 0.6rem",
  border: `1px solid ${palette.border}`,
  borderRadius: radius.sm,
  fontSize: text.sm,
  fontFamily: "inherit",
  background: palette.raised,
  color: palette.text,
};

export const label: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "var(--space-1)",
  fontSize: text.xs,
  color: palette.muted,
  fontWeight: 600,
};

export const button: CSSProperties = {
  padding: "0.4rem 0.85rem",
  border: `1px solid ${palette.border}`,
  borderRadius: radius.sm,
  background: palette.raised,
  color: palette.text,
  fontSize: text.sm,
  fontWeight: 600,
  cursor: "pointer",
};

export const primaryButton: CSSProperties = {
  ...button,
  background: palette.accent,
  // Full `border` shorthand (not `borderColor`) so toggling button↔primaryButton
  // never mixes shorthand + longhand on one element (a React rerender warning).
  border: `1px solid ${palette.accent}`,
  color: palette.onAccent,
};

export const dangerButton: CSSProperties = {
  ...button,
  color: palette.danger,
  border: `1px solid ${palette.dangerBorder}`,
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
  borderRadius: radius.md,
  padding: "var(--space-4)",
  background: palette.raised,
  boxShadow: shadow.sm,
};

export const muted: CSSProperties = {
  color: palette.muted,
  fontSize: text.sm,
};

export const errorText: CSSProperties = {
  color: palette.danger,
  fontSize: text.sm,
};

export const sectionHeading: CSSProperties = {
  fontSize: text.lg,
  margin: "var(--space-5) 0 var(--space-2)",
};

export const chip: CSSProperties = {
  display: "inline-block",
  padding: "0.1rem 0.5rem",
  borderRadius: radius.pill,
  fontSize: text.xs,
  fontWeight: 600,
  background: palette.surface,
  border: `1px solid ${palette.border}`,
  color: palette.text,
};

/** A pill tinted to a semantic status (success/warning/danger/neutral). */
export function statusChip(kind: StatusKind): CSSProperties {
  return {
    ...chip,
    background: status[kind].bg,
    color: status[kind].text,
    border: "1px solid transparent",
  };
}
