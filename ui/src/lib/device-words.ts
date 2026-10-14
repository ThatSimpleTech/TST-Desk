// Wording for the desktop's paired-device list and pairing dialog (TD-5014).
// Pure, so the copy is testable without mounting a component.

function plural(count: number, unit: string): string {
  return `${count} ${unit}${count === 1 ? "" : "s"} ago`;
}

/** "just now", "5 minutes ago", "yesterday"... for a device's last_seen. */
export function relativeWords(iso: string, now: number = Date.now()): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "at an unknown time";
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return plural(minutes, "minute");
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return plural(hours, "hour");
  const days = Math.floor(hours / 24);
  if (days === 1) return "yesterday";
  if (days < 30) return plural(days, "day");
  const months = Math.floor(days / 30);
  if (months < 12) return plural(months, "month");
  return "over a year ago";
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "Paired Oct 14 at 10:40 AM" (the year too when it is not this one), local
 *  time, so two phones with the same name can be told apart. */
export function pairedWords(iso: string, now: number = Date.now()): string {
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "Paired at an unknown time";
  const year = then.getFullYear() === new Date(now).getFullYear() ? "" : `, ${then.getFullYear()}`;
  const hours = then.getHours() % 12 === 0 ? 12 : then.getHours() % 12;
  const minutes = String(then.getMinutes()).padStart(2, "0");
  const half = then.getHours() < 12 ? "AM" : "PM";
  return `Paired ${MONTHS[then.getMonth()]} ${then.getDate()}${year} at ${hours}:${minutes} ${half}`;
}

/** "9:58" for a countdown to the code's expiry. Never negative. */
export function formatCountdown(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}
