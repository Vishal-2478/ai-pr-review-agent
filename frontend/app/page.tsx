"use client";

import Link from "next/link";
import { Problem } from "@/components/Shell";
import { QueueItem, QueueStats, useApi } from "@/lib/api";
import { ago, duration, percent, sentence, shortSha } from "@/lib/format";

interface QueueResponse {
  items: QueueItem[];
  stats: QueueStats;
  alert_thresholds: { escalation_rate: number; queue_depth: number };
}

const KIND: Record<string, string> = {
  escalation: "Critical finding",
  approval: "Needs approval",
  dispute: "Author disputes a finding",
};

export default function QueuePage() {
  const { data, error, loading } = useApi<QueueResponse>("/api/hitl", 10000);
  const stats = data?.stats;
  const tooMany = stats && data && stats.reviews_24h >= 5 && stats.escalation_rate_24h > data.alert_thresholds.escalation_rate;

  return (
    <>
      <h1>Waiting for you</h1>
      <p className="lede">
        Reviews the agent was not sure enough to post on its own. Nothing here reaches a pull request until someone
        decides.
      </p>
      <Problem error={error} />
      {stats ? (
        <dl className="facts">
          <div>
            <dt>Waiting</dt>
            <dd>{stats.pending}</dd>
          </div>
          <div>
            <dt>Longest wait</dt>
            <dd>{stats.pending ? duration(stats.oldest_pending_s) : "none"}</dd>
          </div>
          <div>
            <dt>Decided in the last day</dt>
            <dd>{stats.resolved_24h}</dd>
          </div>
          <div>
            <dt>Reviews that needed a person</dt>
            <dd>
              {stats.reviews_24h ? percent(stats.escalation_rate_24h) : "n/a"}{" "}
              <small>of {stats.reviews_24h} in the last day</small>
            </dd>
          </div>
        </dl>
      ) : null}
      {tooMany ? (
        <div className="notice problem">
          <p>
            More than {percent(data.alert_thresholds.escalation_rate)} of recent reviews needed a person. The agent is
            unsure too often: check the prompts and models before the queue outgrows the people answering it.
          </p>
        </div>
      ) : null}
      {data && data.items.length === 0 ? (
        <p className="empty">Nothing is waiting. Reviews that need a decision appear here.</p>
      ) : null}
      {loading && !data && !error ? <p className="empty">Loading the queue.</p> : null}
      <ul className="rows">
        {(data?.items || []).map((item) => (
          <li key={item.id}>
            <Link className="row-link" href={`/queue/${item.id}`}>
              <span className={`sev ${item.kind === "escalation" ? "critical" : item.kind === "dispute" ? "high" : "medium"}`}>
                {KIND[item.kind] || item.kind}
              </span>
              <span className="what">
                <span className="title">{item.title || `Pull request ${item.pr_number}`}</span>
                <br />
                <span className="meta">
                  {item.repo} #{item.pr_number} at {shortSha(item.head_sha)}, {item.pending_findings}{" "}
                  {item.pending_findings === 1 ? "finding" : "findings"} to decide. {sentence(item.reason)}
                </span>
              </span>
              <span className="when">{ago(item.created_at)}</span>
            </Link>
          </li>
        ))}
      </ul>
    </>
  );
}
