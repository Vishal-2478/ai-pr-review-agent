"use client";

import { useState } from "react";

export interface Datum {
  label: string;
  value: number;
  detail?: string;
}

interface Props {
  data: Datum[];
  format: (v: number) => string;
  ariaLabel: string;
  tickEvery?: number;
}

/** Round up to 1, 2 or 5 times a power of ten, so axis ticks are numbers people say. */
function niceCeiling(value: number): number {
  if (value <= 0) return 1;
  const power = Math.pow(10, Math.floor(Math.log10(value)));
  const step = [1, 2, 5, 10].find((m) => m * power >= value) || 10;
  return step * power;
}

/** One series of thin columns with a hover tooltip. Text stays in ink; only the marks carry colour. */
export function Columns({ data, format, ariaLabel, tickEvery = 6 }: Props) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 720;
  const H = 180;
  const pad = { l: 44, r: 8, t: 14, b: 24 };
  const max = Math.max(...data.map((d) => d.value), 0);
  const top = niceCeiling(max);
  const slot = (W - pad.l - pad.r) / Math.max(1, data.length);
  const bw = Math.min(24, Math.max(2, slot - 2));
  const y = (v: number) => pad.t + (H - pad.t - pad.b) * (1 - v / top);
  const base = H - pad.b;
  const peak = data.reduce((best, d, i) => (d.value > data[best].value ? i : best), 0);

  return (
    <div className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={ariaLabel}>
        {[0.5, 1].map((f) => (
          <g key={f}>
            <line className="grid" x1={pad.l} x2={W - pad.r} y1={y(top * f)} y2={y(top * f)} />
            <text x={pad.l - 6} y={y(top * f) + 4} textAnchor="end">
              {format(top * f)}
            </text>
          </g>
        ))}
        <line className="axis" x1={pad.l} x2={W - pad.r} y1={base} y2={base} />
        {data.map((d, i) => {
          const x = pad.l + i * slot + (slot - bw) / 2;
          const h = Math.max(d.value > 0 ? 1.5 : 0, base - y(d.value));
          const r = Math.min(4, bw / 2, h);
          const path = `M${x},${base} V${base - h + r} Q${x},${base - h} ${x + r},${base - h} H${x + bw - r} Q${x + bw},${base - h} ${x + bw},${base - h + r} V${base} Z`;
          return (
            <g key={i}>
              <rect
                className="hit"
                x={pad.l + i * slot}
                y={pad.t}
                width={slot}
                height={base - pad.t}
                onMouseEnter={() => setHover(i)}
                onMouseLeave={() => setHover(null)}
              />
              {h > 0 ? <path className={`bar${hover === i ? " on" : ""}`} d={path} pointerEvents="none" /> : null}
              {i % tickEvery === 0 ? (
                <text x={pad.l + i * slot + slot / 2} y={H - 8} textAnchor="middle">
                  {d.label}
                </text>
              ) : null}
            </g>
          );
        })}
        {max > 0 && hover === null ? (
          <text className="value" x={pad.l + peak * slot + slot / 2} y={y(data[peak].value) - 5} textAnchor="middle">
            {format(data[peak].value)}
          </text>
        ) : null}
      </svg>
      {hover !== null ? (
        <div
          className="tip"
          style={{ left: `${((pad.l + hover * slot + slot / 2) / W) * 100}%`, top: `${(y(data[hover].value) / H) * 100}%` }}
        >
          {data[hover].detail || data[hover].label}: {format(data[hover].value)}
        </div>
      ) : null}
    </div>
  );
}
