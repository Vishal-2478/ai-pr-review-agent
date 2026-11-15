"use client";

import { Finding } from "@/lib/api";
import { findingWord, percent } from "@/lib/format";

export type Verdict = "accepted" | "rejected";

interface Props {
  finding: Finding;
  verdict?: Verdict;
  onVerdict?: (verdict: Verdict) => void;
}

/** A finding the way a reviewer meets it: a line in the diff, with a mark in the gutter. */
export function FindingCard({ finding: f, verdict, onVerdict }: Props) {
  const lines = (f.evidence || "").split("\n").filter((l) => l.length > 0).slice(0, 8);
  const where = f.line_end > f.line_start ? `${f.file_path}:${f.line_start}-${f.line_end}` : `${f.file_path}:${f.line_start}`;
  const checks: string[] = [];
  checks.push(f.grounded ? "Quoted code found in the change" : "Quoted code not found in the change");
  if (f.source !== "llm") checks.push("Raised by a deterministic check");
  if (f.corroborated_by?.length) checks.push(`Also raised by ${f.corroborated_by.join(", ")}`);

  return (
    <article className={`finding ${f.severity}${verdict === "rejected" ? " is-rejected" : ""}`}>
      <header>
        <span className={`sev ${f.severity}`}>{f.severity}</span>
        <span className="where">{where}</span>
        <span className="meta">
          {f.agent_type}, {f.category}
        </span>
      </header>
      <h3>{f.summary}</h3>
      {lines.length > 0 ? (
        <div className="evidence" aria-label="Code the finding is about">
          <div className="gutter">
            {lines.map((_, i) => (
              <div key={i}>{f.line_start + i}</div>
            ))}
          </div>
          <pre>{lines.join("\n")}</pre>
        </div>
      ) : null}
      <div className="body">
        <p>{f.rationale}</p>
        {f.suggestion ? (
          <p>
            <span className="label">Suggested fix</span>
            <br />
            {f.suggestion}
          </p>
        ) : null}
        <details>
          <summary>
            Confidence {percent(f.confidence)}
            {f.raw_confidence !== null && Math.abs((f.raw_confidence ?? 0) - f.confidence) > 0.005
              ? ` (the reviewer said ${percent(f.raw_confidence)})`
              : ""}
          </summary>
          <ul>
            {checks.map((c) => (
              <li key={c}>{c}</li>
            ))}
            {(f.adjustments || []).map((a) => (
              <li key={a}>{a}</li>
            ))}
          </ul>
        </details>
      </div>
      <footer>
        <span>
          <span className="mono">{f.finding_id}</span> {findingWord(f.status)}
        </span>
        {onVerdict ? (
          <span className="choice" role="group" aria-label={`Decision for ${f.finding_id}`}>
            <button type="button" aria-pressed={verdict !== "rejected"} onClick={() => onVerdict("accepted")}>
              Accept
            </button>
            <button type="button" aria-pressed={verdict === "rejected"} onClick={() => onVerdict("rejected")}>
              Reject
            </button>
          </span>
        ) : null}
      </footer>
    </article>
  );
}
