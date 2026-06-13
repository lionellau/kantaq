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
