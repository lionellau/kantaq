/**
 * Build-time feature flags (Vite env). Kept tiny and explicit — one function
 * per flag, read from `import.meta.env`, so a flag is greppable and a test can
 * stub it with `vi.stubEnv`.
 *
 * `VITE_RECO_PANEL` gates the ticket recommendation panel (E17-T2 / MOD-22).
 * The full right rail that hosts it is E19-T4, "flag-gated until E16/E17 are
 * green"; the panel ships behind this flag (default off) so it can be demoed
 * (`VITE_RECO_PANEL=on pnpm -C web dev`) without claiming the rest of the rail.
 */

export function recoPanelEnabled(): boolean {
  return import.meta.env.VITE_RECO_PANEL === "on";
}
