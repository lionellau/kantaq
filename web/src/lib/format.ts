/** Tiny formatting helpers shared by the tracker screens. */

const formatter = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

/** API timestamps are naive UTC (the MOD-03 encoding); render them as such. */
export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) {
    return "—";
  }
  const date = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`);
  return Number.isNaN(date.getTime()) ? iso : formatter.format(date);
}

/** A capability grant's `expires_at` is unix seconds (the signed-bytes shape). */
export function fmtEpoch(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) {
    return "—";
  }
  return formatter.format(new Date(seconds * 1000));
}

function asDate(iso: string): Date {
  return new Date(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`);
}

/** Coarse "time since" for honest last-seen labels (E20-T7 connection badge). */
export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) {
    return "—";
  }
  const ms = Date.now() - asDate(iso).getTime();
  if (Number.isNaN(ms)) {
    return iso;
  }
  const sec = Math.max(0, Math.floor(ms / 1000));
  if (sec < 45) {
    return "just now";
  }
  const min = Math.floor(sec / 60);
  if (min < 60) {
    return `${min}m ago`;
  }
  const hr = Math.floor(min / 60);
  if (hr < 24) {
    return `${hr}h ago`;
  }
  return `${Math.floor(hr / 24)}d ago`;
}

/** Within the window a real audited call lets a badge say "active" (no optimistic
 *  green past this — the badge must reflect a real session, NFR honesty). */
export function isRecent(iso: string | null | undefined, withinMs = 5 * 60 * 1000): boolean {
  if (!iso) {
    return false;
  }
  const ms = Date.now() - asDate(iso).getTime();
  return !Number.isNaN(ms) && ms >= 0 && ms <= withinMs;
}
