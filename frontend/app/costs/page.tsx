"use client";

import Link from "next/link";
import { useState } from "react";
import { Columns } from "@/components/Columns";
import { Problem } from "@/components/Shell";
import { Economics, useApi } from "@/lib/api";
import { money, percent } from "@/lib/format";

export default function CostsPage() {
  const { data, error, loading } = useApi<Economics>("/api/economics/summary?hours=24", 30000);
  const [asTable, setAsTable] = useState(false);

  if (error) return <Problem error={error} />;
  if (loading && !data) return <p className="empty">Loading costs.</p>;
  if (!data) return null;

  const b = data.budget;
  // A continuous 48-hour axis: hours with no spend are zero, not missing.
  const spendByHour = new Map(data.timeseries.map((p) => [new Date(p.t).setMinutes(0, 0, 0), p]));
  const thisHour = new Date().setMinutes(0, 0, 0);
  const hours = Array.from({ length: 48 }, (_, i) => {
    const at = thisHour - (47 - i) * 3600_000;
    const d = new Date(at);
    const point = spendByHour.get(at);
    const clock = `${String(d.getHours()).padStart(2, "0")}:00`;
    return {
      label: clock,
      value: point?.cost_usd || 0,
      detail: `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${clock}, ${point?.llm_calls || 0} model calls`,
    };
  });
  const anySpend = hours.some((h) => h.value > 0);
  const totalAgents = data.agents.reduce((sum, a) => sum + a.cost_usd, 0);
  const specialists = ["security", "quality", "tests", "docs"];
  const agents = data.agents
    .filter((a) => a.llm_calls > 0 || a.cost_usd > 0 || a.errors > 0 || a.feedback_total > 0)
    .sort((a, c) => c.cost_usd - a.cost_usd);

  return (
    <>
      <h1>Costs</h1>
      <p className="lede">
        What the agents spent, read from the same rollups the budget guard uses to stop a runaway day.
      </p>

      <dl className="facts">
        <div>
          <dt>Spent today</dt>
          <dd>
            {money(b.spent_today)} <small>of {b.daily_cap > 0 ? money(b.daily_cap) : "no cap"}</small>
          </dd>
        </div>
        <div>
          <dt>Cap per review</dt>
          <dd>{b.per_review_cap > 0 ? money(b.per_review_cap) : "none"}</dd>
        </div>
        <div>
          <dt>Model calls, last {data.window_hours} h</dt>
          <dd>{data.agents.reduce((sum, a) => sum + a.llm_calls, 0)}</dd>
        </div>
      </dl>
      {b.daily_cap > 0 ? (
        <div
          className={`meter${b.ratio >= 1 ? " over" : ""}`}
          role="meter"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(Math.min(1, b.ratio) * 100)}
          aria-label="Share of today's budget spent"
        >
          <span style={{ width: `${Math.min(100, b.ratio * 100)}%` }} />
        </div>
      ) : null}
      {!b.allowed ? (
        <div className="notice problem" style={{ marginTop: "1rem" }}>
          <p>
            Budget reached: {b.reason}. Reviews continue on the deterministic checks only until the cap resets or you
            raise DAILY_BUDGET_USD.
          </p>
        </div>
      ) : b.downgrade ? (
        <div className="notice" style={{ marginTop: "1rem" }}>
          <p>{percent(b.ratio)} of today's budget is spent. Agents now start on the cheaper fallback model.</p>
        </div>
      ) : null}

      <h2>Spend per hour, last 48 hours</h2>
      {!anySpend ? (
        <p className="empty">No model spend in the last 48 hours.</p>
      ) : asTable ? (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Hour</th>
                <th className="num">Spend</th>
              </tr>
            </thead>
            <tbody>
              {hours.filter((h) => h.value > 0).map((h) => (
                <tr key={h.detail}>
                  <td>{h.detail}</td>
                  <td className="num">{money(h.value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Columns data={hours} format={money} ariaLabel="Model spend per hour over the last 48 hours" />
      )}
      {anySpend ? (
        <button className="quiet" onClick={() => setAsTable(!asTable)}>
          {asTable ? "Show as chart" : "Show as table"}
        </button>
      ) : null}

      <h2>By agent, last {data.window_hours} hours</h2>
      {agents.length === 0 ? <p className="empty">No agent has called a model in this window.</p> : null}
      <div className="scroll" hidden={agents.length === 0}>
        <table>
          <thead>
            <tr>
              <th>Agent</th>
              <th>Model</th>
              <th className="num">Calls</th>
              <th className="num">Cost</th>
              <th style={{ width: "24%" }}>Share of spend</th>
              <th className="num">Slowest 5%</th>
              <th className="num">Rejected by people</th>
              <th className="num">Errors</th>
            </tr>
          </thead>
          <tbody>
            {agents.map((a) => (
              <tr key={a.agent}>
                <td>{a.agent}</td>
                <td className="mono">{specialists.includes(a.agent) ? data.routing.models[a.agent] : ""}</td>
                <td className="num">{a.llm_calls}</td>
                <td className="num">{money(a.cost_usd)}</td>
                <td>
                  <div className="meter" title={percent(totalAgents ? a.cost_usd / totalAgents : 0)}>
                    <span style={{ width: `${totalAgents ? (a.cost_usd / totalAgents) * 100 : 0}%` }} />
                  </div>
                </td>
                <td className="num">{a.p95_latency_ms ? `${(a.p95_latency_ms / 1000).toFixed(1)} s` : ""}</td>
                <td className="num">
                  {a.feedback_total ? `${a.feedback_negative} of ${a.feedback_total}` : ""}
                </td>
                <td className="num">{a.errors || ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.routing.unpriced.length > 0 ? (
        <p className="meta" style={{ marginTop: "0.5rem" }}>
          No price is configured for {data.routing.unpriced.join(", ")}, so a conservative default is used. Add it to
          backend/economics/pricing.json.
        </p>
      ) : null}

      {data.advice.length > 0 ? (
        <>
          <h2>Worth changing</h2>
          <ul>
            {data.advice.map((a) => (
              <li key={a.agent + a.kind}>
                <strong>{a.agent}:</strong> {a.message}
              </li>
            ))}
          </ul>
        </>
      ) : null}

      <h2>Most expensive reviews, last 7 days</h2>
      {data.top_reviews.length === 0 ? <p className="empty">No reviews with model spend yet.</p> : null}
      {data.top_reviews.length > 0 ? (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Pull request</th>
                <th className="num">Tokens in</th>
                <th className="num">Tokens out</th>
                <th className="num">Cost</th>
              </tr>
            </thead>
            <tbody>
              {data.top_reviews.map((r) => (
                <tr key={r.review_id}>
                  <td>
                    <Link href={`/reviews/${r.review_id}`}>{r.title || r.review_id.slice(0, 8)}</Link>
                    {r.repo ? (
                      <div className="meta">
                        {r.repo} #{r.pr_number}
                      </div>
                    ) : null}
                  </td>
                  <td className="num">{(r.tokens_in || 0).toLocaleString()}</td>
                  <td className="num">{(r.tokens_out || 0).toLocaleString()}</td>
                  <td className="num">{money(r.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  );
}
