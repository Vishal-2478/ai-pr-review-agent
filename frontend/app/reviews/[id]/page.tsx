"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { FindingCard } from "@/components/FindingCard";
import { Problem } from "@/components/Shell";
import { Finding, QueueItem, Review, TraceEvent, useApi } from "@/lib/api";
import { money, percent, sentence, shortSha, statusWord } from "@/lib/format";

interface Detail {
  review: Review;
  findings: Finding[];
  hitl: QueueItem[];
}

interface Trace {
  events: TraceEvent[];
  totals: { events: number; llm_calls: number; cost_usd: number; tokens_in: number; tokens_out: number; errors: number };
}

interface Span {
  id: string;
  agent: string;
  name: string;
  start: number;
  end: number;
  outcome: string;
  depth: number;
}

function buildSpans(events: TraceEvent[]): { spans: Span[]; t0: number; t1: number } {
  const starts = new Map<string, TraceEvent>();
  const parents = new Map<string, string | null>();
  const spans: Span[] = [];
  for (const e of events) {
    if (!e.span_id) continue;
    if (e.event_type === "span.start") {
      starts.set(e.span_id, e);
      parents.set(e.span_id, e.parent_span);
    } else if (e.event_type === "span.end") {
      const s = starts.get(e.span_id);
      const end = new Date(e.ts).getTime();
      const start = s ? new Date(s.ts).getTime() : end - (e.latency_ms || 0);
      spans.push({ id: e.span_id, agent: e.agent, name: e.name || "", start, end, outcome: e.outcome || "", depth: 0 });
    }
  }
  for (const span of spans) {
    let depth = 0;
    let parent = parents.get(span.id) || null;
    while (parent && depth < 6) {
      depth += 1;
      parent = parents.get(parent) || null;
    }
    span.depth = depth;
  }
  spans.sort((a, b) => a.start - b.start || a.depth - b.depth);
  const t0 = Math.min(...spans.map((s) => s.start));
  const t1 = Math.max(...spans.map((s) => s.end));
  return { spans, t0, t1 };
}

export default function ReviewPage() {
  const { id } = useParams<{ id: string }>();
  const detail = useApi<Detail>(`/api/reviews/${id}`, 10000);
  const trace = useApi<Trace>(`/api/reviews/${id}/trace`, 10000);
  const [showEvents, setShowEvents] = useState(false);

  if (detail.error) return <Problem error={detail.error} />;
  if (!detail.data) return <p className="empty">Loading the review.</p>;

  const { review, findings, hitl } = detail.data;
  const shown = findings.filter((f) => f.status !== "suppressed");
  const suppressed = findings.filter((f) => f.status === "suppressed");
  const timeline = trace.data && trace.data.events.length ? buildSpans(trace.data.events) : null;
  const total = timeline ? Math.max(1, timeline.t1 - timeline.t0) : 1;

  return (
    <>
      <p className="meta">
        <Link href="/reviews">All reviews</Link>
      </p>
      <h1>{review.title || `Pull request ${review.pr_number}`}</h1>
      <p className="lede">
        {review.repo} #{review.pr_number} at <span className="mono">{shortSha(review.head_sha)}</span> by{" "}
        {review.author || "unknown"}. {review.html_url ? <a href={review.html_url}>Open the pull request</a> : null}
      </p>

      <dl className="facts">
        <div>
          <dt>Outcome</dt>
          <dd>{statusWord(review.status)}</dd>
        </div>
        <div>
          <dt>Confidence</dt>
          <dd>{percent(review.overall_confidence)}</dd>
        </div>
        <div>
          <dt>Specialists finished</dt>
          <dd>{percent(review.coverage)}</dd>
        </div>
        <div>
          <dt>Cost</dt>
          <dd>{money(trace.data?.totals.cost_usd ?? review.cost_usd)}</dd>
        </div>
      </dl>

      {(review.reasons || []).length > 0 ? (
        <div className="notice">
          <p>
            <strong>Why the agent decided this</strong>
          </p>
          <ul style={{ margin: 0, paddingLeft: "1.1rem" }}>
            {review.reasons.map((r) => (
              <li key={r}>{sentence(r)}</li>
            ))}
          </ul>
          {hitl.length > 0 ? (
            <p style={{ marginTop: "0.5rem" }}>
              <Link href={`/queue/${hitl[0].id}`}>Decide on this review</Link>
            </p>
          ) : null}
        </div>
      ) : null}
      {review.error ? (
        <div className="notice problem">
          <p>{review.error}</p>
        </div>
      ) : null}

      <h2>Findings ({shown.length})</h2>
      {shown.length === 0 ? <p className="empty">Nothing met the bar for attention.</p> : null}
      {shown.map((f) => (
        <FindingCard key={f.finding_id} finding={f} />
      ))}
      {suppressed.length > 0 ? (
        <details>
          <summary>
            {suppressed.length} suppressed as noise
          </summary>
          <div style={{ marginTop: "0.75rem" }}>
            {suppressed.map((f) => (
              <FindingCard key={f.finding_id} finding={f} />
            ))}
          </div>
        </details>
      ) : null}

      <h2>Trace</h2>
      <Problem error={trace.error} />
      {trace.data ? (
        <p className="meta">
          {trace.data.totals.events} events, {trace.data.totals.llm_calls} model calls,{" "}
          {trace.data.totals.tokens_in.toLocaleString()} tokens in and {trace.data.totals.tokens_out.toLocaleString()} out
          {trace.data.totals.errors ? `, ${trace.data.totals.errors} errors` : ""}.
        </p>
      ) : null}
      {timeline ? (
        <div className="trace" role="table" aria-label="Steps of this review on a timeline">
          {timeline.spans.map((s) => {
            const bad = !["ok", "degraded", ""].includes(s.outcome);
            return (
              <div className="span" key={s.id} role="row" title={`${s.agent} ${s.name}: ${s.end - s.start} ms, ${s.outcome}`}>
                <span className="name" style={{ paddingLeft: `${s.depth * 0.9}rem` }} role="cell">
                  {s.agent} {s.name}
                  {s.outcome && s.outcome !== "ok" ? <span className="meta"> ({s.outcome})</span> : null}
                </span>
                <span className="track" role="cell">
                  <i
                    className={bad ? "bad" : undefined}
                    style={{
                      left: `${((s.start - timeline.t0) / total) * 100}%`,
                      width: `${Math.max(0.5, ((s.end - s.start) / total) * 100)}%`,
                    }}
                  />
                </span>
                <span className="took" role="cell">
                  {s.end - s.start} ms
                </span>
              </div>
            );
          })}
        </div>
      ) : null}

      {trace.data ? (
        <p style={{ marginTop: "1rem" }}>
          <button className="quiet" onClick={() => setShowEvents(!showEvents)} aria-expanded={showEvents}>
            {showEvents ? "Hide every event" : "Show every event"}
          </button>
        </p>
      ) : null}
      {showEvents && trace.data ? (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Who</th>
                <th>What</th>
                <th>Result</th>
                <th className="num">Tokens</th>
                <th className="num">Cost</th>
              </tr>
            </thead>
            <tbody>
              {trace.data.events.map((e, i) => (
                <tr key={i}>
                  <td className="mono">{e.ts.slice(11, 23)}</td>
                  <td>{e.agent}</td>
                  <td>
                    <details>
                      <summary>
                        {e.event_type} {e.name || ""} {e.model ? `(${e.model})` : ""}
                      </summary>
                      <pre className="payload">{JSON.stringify(e.payload, null, 2)}</pre>
                    </details>
                  </td>
                  <td>
                    {e.outcome || ""}
                    {e.latency_ms !== null && e.latency_ms !== undefined ? ` ${e.latency_ms} ms` : ""}
                  </td>
                  <td className="num">{e.tokens_in ? `${e.tokens_in} / ${e.tokens_out}` : ""}</td>
                  <td className="num">{e.cost_usd ? money(e.cost_usd) : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  );
}
