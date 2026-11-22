import { useState } from 'react';
import { fmtClock, fmtMs, fmtN, STAGES } from '../format';
import { useWidth } from '../hooks';
import type { Snapshot } from '../types';
import { useTooltip } from './Tooltip';


/** Bar with a 4px rounded data end and a flush baseline: 'r' rounds the right end, 't' rounds the top end. */
function barPath(x: number, y: number, w: number, h: number, end: 'r' | 't', r = 4): string {
  const k = Math.min(r, w / 2, h / 2);
  return end === 'r'
    ? `M${x} ${y}H${x + w - k}Q${x + w} ${y} ${x + w} ${y + k}V${y + h - k}Q${x + w} ${y + h} ${x + w - k} ${y + h}H${x}Z`
    : `M${x} ${y + h}V${y + k}Q${x} ${y} ${x + k} ${y}H${x + w - k}Q${x + w} ${y} ${x + w} ${y + k}V${y + h}Z`;
}

const Empty = ({ text }: { text: string }) => <div className="empty">{text}</div>;

/** Median bar per stage with a 95th-percentile tick. One colour per stage (same as the lanes). */
export function StageLatency({ snap }: { snap: Snapshot | null }) {
  const [ref, W] = useWidth<HTMLDivElement>(280);
  const tip = useTooltip();
  const stages = snap?.stages ?? {};
  const rows = STAGES.filter((s) => stages[s.key]);
  if (!snap?.run || !rows.length) return <div ref={ref}><Empty text="No stage timings yet." /></div>;
  const max = Math.max(...rows.map((r) => stages[r.key].p95), 1), L = 62, R = 112, rh = 38, H = rows.length * rh + 6;
  const x = (v: number) => L + (v / max) * (W - L - R);
  return (
    <div ref={ref}>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} height={H} role="img" aria-label="Median and 95th percentile latency per stage">
        {rows.map((r, i) => {
          const s = stages[r.key], y = i * rh + 4;
          return (
            <g key={r.key}>
              <text className="vlabel" x={0} y={y + 14}>{r.name}</text>
              <line x1={L} x2={W - R} y1={y + 22} y2={y + 22} stroke="var(--rule)" />
              <path className="sbar" style={{ '--i': i } as React.CSSProperties} d={barPath(L, y + 8, Math.max(2, x(s.p50) - L), 10, 'r')} fill={r.color}
                    onMouseMove={(e) => tip.show(<><b>{r.name}</b><br />{s.count} runs<br />median {fmtMs(s.p50)}<br />95th {fmtMs(s.p95)}<br />max {fmtMs(s.max)}</>, e)} onMouseLeave={tip.hide} />
              <line x1={x(s.p95)} x2={x(s.p95)} y1={y + 4} y2={y + 22} stroke="var(--ink)" strokeWidth={2} />
              <text className="vlabel num" x={W - R + 10} y={y + 14}>{fmtMs(s.p50)}</text>
              <text className="vsub num" x={W - R + 10} y={y + 29}>95th {fmtMs(s.p95)}, n={s.count}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

/** Helpdesk calls per second as a thin line, with the provider's limit drawn as a labelled hairline. */
export function RequestsPerSecond({ snap }: { snap: Snapshot | null }) {
  const [ref, W] = useWidth<HTMLDivElement>(260);
  const tip = useTooltip();
  const [hoverI, setHoverI] = useState<number | null>(null);
  const pts = (snap?.http?.per_second ?? []).slice(-90);
  if (!snap?.run || !pts.length) return <div ref={ref}><Empty text="No requests yet." /></div>;
  const lim = snap.http!.limit_rps, H = 190, L = 28, R = 8, T = 10, B = 22;
  const ymax = Math.max(lim + 1, ...pts.map((p) => p.n)) * 1.1;
  const x = (i: number) => L + (i / Math.max(1, pts.length - 1)) * (W - L - R), y = (v: number) => T + (1 - v / ymax) * (H - T - B);
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)} ${y(p.n).toFixed(1)}`).join(' ');
  const last = pts[pts.length - 1];
  return (
    <div ref={ref}>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} height={H} role="img" aria-label="Helpdesk requests per second against the limit">
        <g className="grid">{[0, Math.round(ymax / 2), Math.floor(ymax)].map((v) => <line key={v} x1={L} x2={W - R} y1={y(v)} y2={y(v)} />)}</g>
        <g className="axis">{[0, Math.round(ymax / 2), Math.floor(ymax)].map((v) => <text key={v} x={L - 6} y={y(v) + 4} textAnchor="end">{v}</text>)}</g>
        <path d={`${d} L${x(pts.length - 1)} ${y(0)} L${x(0)} ${y(0)}z`} fill="var(--s1)" opacity={0.12} />
        <path className="line" pathLength={1} d={d} fill="none" stroke="var(--s1)" strokeWidth={2} strokeLinejoin="round" />
        <line className="limit" x1={L} x2={W - R} y1={y(lim)} y2={y(lim)} />
        <text className="limit-label" x={W - R} y={y(lim) - 5} textAnchor="end">limit {lim} per second</text>
        {hoverI != null && <line x1={x(hoverI)} x2={x(hoverI)} y1={T} y2={H - B} stroke="var(--ink-3)" />}
        <circle className="pt" cx={x(pts.length - 1)} cy={y(last.n)} r={3.5} fill="var(--s1)" stroke="var(--surface)" strokeWidth={2} />
        <rect x={L} y={T} width={W - L - R} height={H - T - B} fill="transparent"
              onMouseMove={(e) => {
                const r = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect();
                const i = Math.max(0, Math.min(pts.length - 1, Math.round(((e.clientX - r.left - L) / (W - L - R)) * (pts.length - 1))));
                setHoverI(i); tip.show(<><b>{pts[i].n}</b> requests at {fmtClock(pts[i].t)}</>, e);
              }}
              onMouseLeave={() => { setHoverI(null); tip.hide(); }} />
      </svg>
    </div>
  );
}

/** Model tokens per minute against the quota the worker pool is paced under. */
export function TokensPerMinute({ snap }: { snap: Snapshot | null }) {
  const [ref, W] = useWidth<HTMLDivElement>(260);
  const tip = useTooltip();
  const ser = (snap?.tokens_per_minute?.series ?? []).slice(-12);
  if (!snap?.run || !ser.length) return <div ref={ref}><Empty text="No model calls yet." /></div>;
  const limit = snap.tokens_per_minute!.limit, H = 190, L = 40, R = 8, T = 10, B = 22;  // null: this provider has no per-minute token window
  const peakTokens = Math.max(1, ...ser.map((s) => s.tokens)), lim = limit ?? peakTokens;
  const ymax = Math.max(lim * 1.1, ...ser.map((s) => s.tokens)), y = (v: number) => T + (1 - v / ymax) * (H - T - B), bw = (W - L - R) / ser.length;
  const ticks = [0, lim / 2, lim];
  return (
    <div ref={ref}>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} height={H} role="img" aria-label={limit === null ? 'Decision tokens per minute' : 'Decision tokens per minute against the limit'}>
        <g className="grid">{ticks.map((v) => <line key={v} x1={L} x2={W - R} y1={y(v)} y2={y(v)} />)}</g>
        <g className="axis">{ticks.map((v) => <text key={v} x={L - 6} y={y(v) + 4} textAnchor="end">{v >= 1000 ? `${v / 1000}k` : v}</text>)}</g>
        {ser.map((s, i) => {
          const h = Math.max(1, y(0) - y(s.tokens));
          return (
            <g key={s.minute}>
              <path className="tbar" style={{ '--i': i } as React.CSSProperties} d={barPath(L + i * bw + 2, y(0) - h, Math.max(2, bw - 4), h, 't')} fill="var(--s1)"
                    onMouseMove={(e) => tip.show(<>minute {s.minute + 1}<br /><b>{fmtN(s.tokens)}</b> tokens</>, e)} onMouseLeave={tip.hide} />
              <text className="vsub" x={L + i * bw + bw / 2} y={H - 6} textAnchor="middle">{s.minute + 1}</text>
            </g>
          );
        })}
        {limit !== null && <line className="limit" x1={L} x2={W - R} y1={y(lim)} y2={y(lim)} />}
        {limit !== null && <text className="limit-label" x={W - R} y={y(lim) - 5} textAnchor="end">limit {fmtN(lim)} per minute</text>}
      </svg>
    </div>
  );
}

/** Ranked bars for a nominal breakdown: one series, one colour. */
export function Decisions({ snap }: { snap: Snapshot | null }) {
  const [ref, W] = useWidth<HTMLDivElement>(260);
  const tip = useTooltip();
  const items = Object.entries(snap?.progress?.actions ?? {}).sort((a, b) => b[1] - a[1]);
  if (!snap?.run || !items.length) return <div ref={ref}><Empty text="Nothing decided yet." /></div>;
  const max = Math.max(...items.map((i) => i[1])), tot = items.reduce((s, i) => s + i[1], 0), rh = 44, H = items.length * rh, L = 70, R = 70;
  return (
    <div ref={ref}>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} height={H} role="img" aria-label="Decisions by action">
        {items.map(([name, n], i) => {
          const y = i * rh + 8, w = Math.max(3, (n / max) * (W - L - R));
          return (
            <g key={name}>
              <text className="vlabel" x={0} y={y + 15}>{name}</text>
              <path className="dbar" style={{ '--i': i } as React.CSSProperties} d={barPath(L, y + 3, w, 16, 'r')} fill="var(--s1)"
                    onMouseMove={(e) => tip.show(<><b>{n}</b> of {tot} tickets ({Math.round((n / tot) * 100)}%)</>, e)} onMouseLeave={tip.hide} />
              <text className="vlabel num" x={L + w + 8} y={y + 16}>{n}</text>
              <text className="vsub num" x={L + w + 8 + String(n).length * 8 + 4} y={y + 16}>{Math.round((n / tot) * 100)}%</text>
            </g>
          );
        })}
      </svg>
      <div className="hint" style={{ marginTop: 10 }}>Outcomes: {Object.entries(snap.progress!.outcomes).map(([k, v]) => `${v} ${k}`).join(', ')}</div>
    </div>
  );
}
