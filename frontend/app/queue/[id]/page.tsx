"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { FindingCard, Verdict } from "@/components/FindingCard";
import { Problem } from "@/components/Shell";
import { ApiError, Finding, QueueItem, Review, api, useApi } from "@/lib/api";
import { ago, percent, sentence, shortSha } from "@/lib/format";

interface Detail {
  item: QueueItem & { note?: string; reviewer?: string };
  review: Review;
  findings: Finding[];
}

interface Outcome {
  status: string;
  accepted?: number;
  rejected?: number;
  publishing?: boolean;
}

function published(accepted: number, rejected: number, keptPrivate: number): string {
  const count = (n: number, noun: string) => `${n} ${noun}${n === 1 ? "" : "s"}`;
  const posted = Math.max(0, accepted - keptPrivate);
  const parts: string[] = [];
  if (posted > 0) parts.push(`Publishing ${count(posted, "accepted finding")} to the pull request.`);
  if (keptPrivate > 0) {
    parts.push(
      `${count(keptPrivate, "critical security finding")} accepted and kept private${posted > 0 ? "." : ": nothing is posted on the pull request."}`,
    );
  }
  parts.push(`${rejected} rejected.`);
  return parts.join(" ");
}

export default function DecidePage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { data, error } = useApi<Detail>(`/api/hitl/${id}`);
  const [verdicts, setVerdicts] = useState<Record<string, Verdict>>({});
  const [note, setNote] = useState("");
  const [publishWithheld, setPublishWithheld] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<ApiError | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [keptPrivate, setKeptPrivate] = useState(0);

  if (error) return <Problem error={error} />;
  if (!data) return <p className="empty">Loading the review.</p>;

  const { item, review } = data;
  const isDispute = item.kind === "dispute";
  const open = item.status === "pending";
  const toDecide = data.findings.filter((f) => (isDispute ? true : f.status === "pending_human"));
  const others = data.findings.filter((f) => !toDecide.includes(f) && f.status !== "suppressed");
  const accepted = toDecide.filter((f) => verdicts[f.finding_id] !== "rejected");
  const hasPrivate = accepted.some((f) => f.severity === "critical" && f.agent_type === "security");

  async function decide(action: "approve" | "reject") {
    setBusy(true);
    setFailure(null);
    try {
      const body = {
        action,
        note,
        publish_withheld: publishWithheld,
        findings: Object.entries(verdicts).map(([finding_id, verdict]) => ({ finding_id, verdict })),
      };
      // Counted now, because the findings change status as soon as the decision lands.
      const privateNow = publishWithheld
        ? 0
        : accepted.filter((f) => f.severity === "critical" && f.agent_type === "security").length;
      setOutcome(await api<Outcome>(`/api/hitl/${id}/decision`, { method: "POST", body: JSON.stringify(body) }));
      setKeptPrivate(action === "approve" ? privateNow : 0);
    } catch (e) {
      setFailure(e as ApiError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <p className="meta">
        <Link href="/">Waiting for you</Link>
      </p>
      <h1>{review.title || `Pull request ${review.pr_number}`}</h1>
      <p className="lede">
        {review.repo} #{review.pr_number} at <span className="mono">{shortSha(review.head_sha)}</span> by{" "}
        {review.author || "unknown"}, queued {ago(item.created_at)}.{" "}
        {review.html_url ? <a href={review.html_url}>Open the pull request</a> : null}
      </p>

      <div className={`notice${item.kind === "escalation" ? " problem" : ""}`}>
        <p>
          <strong>
            {isDispute
              ? `${item.requested_by || "The author"} disputes this finding.`
              : item.kind === "escalation"
                ? "A critical finding always goes to a person, whatever the confidence."
                : "The agent was not confident enough to post this on its own."}
          </strong>
        </p>
        <p>
          {sentence(item.reason)}
          {review.overall_confidence !== null ? ` Overall confidence ${percent(review.overall_confidence)}.` : ""}
          {review.coverage !== null && review.coverage < 1
            ? ` Only ${percent(review.coverage)} of the specialists finished normally.`
            : ""}
        </p>
        <p>
          <Link href={`/reviews/${review.id}`}>See how the agent got here</Link>
        </p>
      </div>

      {outcome ? (
        <div className="notice done">
          <p>
            {isDispute
              ? outcome.status === "approved"
                ? "Recorded: the finding stands."
                : "Recorded: the finding is retracted and the author has been told."
              : outcome.publishing
                ? published(outcome.accepted || 0, outcome.rejected || 0, keptPrivate)
                : "Rejected. Nothing was posted."}{" "}
            Your decision is saved as feedback and adjusts how confident the agent is next time.
          </p>
          <div className="actions">
            <button className="primary" onClick={() => router.push("/")}>
              Back to the queue
            </button>
          </div>
        </div>
      ) : null}
      {!open && !outcome ? (
        <div className="notice">
          <p>
            Already {item.status}
            {item.reviewer ? ` by ${item.reviewer}` : ""}.
          </p>
        </div>
      ) : null}

      <h2>{isDispute ? "The disputed finding" : `Findings to decide (${toDecide.length})`}</h2>
      {toDecide.map((f) => (
        <FindingCard
          key={f.finding_id}
          finding={f}
          verdict={isDispute ? undefined : verdicts[f.finding_id] || "accepted"}
          onVerdict={open && !outcome && !isDispute ? (v) => setVerdicts({ ...verdicts, [f.finding_id]: v }) : undefined}
        />
      ))}

      {open && !outcome ? (
        <>
          <label htmlFor="note">
            Note <span className="hint">Optional. Kept in the audit trail.</span>
          </label>
          <textarea id="note" rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
          {hasPrivate && !isDispute ? (
            <p style={{ marginTop: "0.75rem" }}>
              <label style={{ fontWeight: 400 }}>
                <input type="checkbox" checked={publishWithheld} onChange={(e) => setPublishWithheld(e.target.checked)} />{" "}
                Also post the critical security finding on the pull request.{" "}
                <span className="hint">Left unticked, it stays private so the weakness is not advertised.</span>
              </label>
            </p>
          ) : null}
          <Problem error={failure} />
          <div className="actions">
            {isDispute ? (
              <>
                <button className="primary" disabled={busy} onClick={() => decide("approve")}>
                  Finding stands
                </button>
                <button disabled={busy} onClick={() => decide("reject")}>
                  Retract finding
                </button>
              </>
            ) : (
              <>
                <button className="primary" disabled={busy || accepted.length === 0} onClick={() => decide("approve")}>
                  Publish {accepted.length} accepted {accepted.length === 1 ? "finding" : "findings"}
                </button>
                <button disabled={busy} onClick={() => decide("reject")}>
                  Reject all
                </button>
              </>
            )}
          </div>
        </>
      ) : null}

      {others.length > 0 ? (
        <>
          <h2>Other findings in this review</h2>
          {others.map((f) => (
            <FindingCard key={f.finding_id} finding={f} />
          ))}
        </>
      ) : null}
    </>
  );
}
