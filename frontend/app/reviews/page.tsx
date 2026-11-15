"use client";

import Link from "next/link";
import { Problem } from "@/components/Shell";
import { Review, useApi } from "@/lib/api";
import { ago, money, percent, shortSha, statusWord } from "@/lib/format";

export default function ReviewsPage() {
  const { data, error, loading } = useApi<{ reviews: Review[] }>("/api/reviews?limit=100", 15000);
  return (
    <>
      <h1>All reviews</h1>
      <p className="lede">Every commit the agent looked at, newest first. Open one to see its findings and trace.</p>
      <Problem error={error} />
      {loading && !data && !error ? <p className="empty">Loading reviews.</p> : null}
      {data && data.reviews.length === 0 ? (
        <p className="empty">No reviews yet. Open a pull request on a connected repository to start one.</p>
      ) : null}
      {data && data.reviews.length > 0 ? (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Pull request</th>
                <th>Outcome</th>
                <th className="num">Confidence</th>
                <th className="num">Cost</th>
                <th className="num">When</th>
              </tr>
            </thead>
            <tbody>
              {data.reviews.map((r) => (
                <tr key={r.id}>
                  <td>
                    <Link href={`/reviews/${r.id}`}>{r.title || `Pull request ${r.pr_number}`}</Link>
                    <div className="meta">
                      {r.repo} #{r.pr_number} at <span className="mono">{shortSha(r.head_sha)}</span>
                    </div>
                  </td>
                  <td>
                    {statusWord(r.status)}
                    {r.error ? <div className="meta">{r.error}</div> : null}
                  </td>
                  <td className="num">{percent(r.overall_confidence)}</td>
                  <td className="num">{money(r.cost_usd)}</td>
                  <td className="num">{ago(r.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  );
}
