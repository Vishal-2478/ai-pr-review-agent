export function money(value: number | null | undefined): string {
  const v = Number(value || 0);
  if (v === 0) return "$0";
  if (v < 0.01) return `$${v.toFixed(4)}`;
  return `$${v.toFixed(2)}`;
}

export function percent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "n/a";
  return `${Math.round(Number(value) * 100)}%`;
}

export function duration(seconds: number | null | undefined): string {
  const s = Math.max(0, Math.round(Number(seconds || 0)));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} days`;
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  return `${duration((Date.now() - then) / 1000)} ago`;
}

export function shortSha(sha: string): string {
  return (sha || "").slice(0, 7);
}

const STATUS_WORDS: Record<string, string> = {
  queued: "Queued",
  running: "Reviewing",
  posted: "Posted",
  pending_human: "Waiting for a person",
  escalated: "Escalated",
  rejected: "Rejected by a person",
  resolved: "Handled privately",
  clean: "Nothing to report",
  shadow: "Recorded only",
  failed: "Failed",
  superseded: "Replaced by a newer commit",
  skipped: "Skipped",
};

export function statusWord(status: string): string {
  return STATUS_WORDS[status] || status;
}

const FINDING_WORDS: Record<string, string> = {
  proposed: "Ready to post",
  suppressed: "Suppressed as noise",
  pending_human: "Waiting for a decision",
  posted: "Posted",
  withheld: "Kept private",
  rejected: "Rejected",
  disputed: "Disputed by the author",
  upheld: "Dispute rejected",
  retracted: "Retracted",
};

export function findingWord(status: string): string {
  return FINDING_WORDS[status] || status;
}

/** Gate reasons are stored as fragments; show them as sentences. */
export function sentence(text: string): string {
  const t = (text || "").trim().replace(/[.\s]+$/, "");
  return t ? `${t.charAt(0).toUpperCase()}${t.slice(1)}.` : "";
}
