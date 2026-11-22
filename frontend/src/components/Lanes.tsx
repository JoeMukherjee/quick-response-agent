import { fmtClock, fmtMs, niceTicks, STAGE_COLOR, STAGE_NAME, STAGES } from '../format';
import { useWidth } from '../hooks';
import type { Snapshot } from '../types';
import { useTooltip } from './Tooltip';

export function Legend() {
  return (
    <div className="legend">
      {STAGES.map((s) => <span key={s.key}><i style={{ background: s.color }} />{s.name}</span>)}
    </div>
  );
}

/** One row per worker thread; every rectangle is a real pipeline stage with its real start and end. */
export function Lanes({ snap }: { snap: Snapshot | null }) {
  const [ref, W] = useWidth<HTMLDivElement>(420);
  const tip = useTooltip();
  const lanes = snap?.lanes ?? [];
  if (!snap?.run || !lanes.length) {
    return <div ref={ref} className="lanes"><div className="empty"><b>No worker activity yet.</b><br />Start a dry run from Bench, or run <span className="mono">python -m qra run</span>.</div></div>;
  }
  const L = 70, R = 10, T = 6, rowH = 28, H = T + lanes.length * rowH + 26;
  const elapsed = snap.run.elapsed, live = snap.run.status === 'running';
  const win = live ? Math.max(60, Math.min(elapsed, 120)) : Math.max(5, elapsed);
  const t1 = live ? elapsed : Math.max(elapsed, 5), t0 = Math.max(0, t1 - win);
  const x = (t: number) => L + ((Math.max(t0, Math.min(t1, t)) - t0) / (t1 - t0)) * (W - L - R);
  return (
    <div ref={ref} className="lanes">
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} height={H} role="img" aria-label="Timeline of each worker thread">
        <g className="grid">{niceTicks(t0, t1, 6).map((t) => <line key={t} x1={x(t)} x2={x(t)} y1={T} y2={H - 22} />)}</g>
        <g className="axis">{niceTicks(t0, t1, 6).map((t) => <text key={t} x={x(t)} y={H - 6} textAnchor="middle">{fmtClock(t)}</text>)}</g>
        {lanes.map((ln, i) => {
          const y = T + i * rowH;
          return (
            <g key={ln.thread}>
              <text className="lane-label" x={0} y={y + 17}>{ln.thread.replace('qra-worker-', 'worker ')}</text>
              <line x1={L} x2={W - R} y1={y + rowH - 1} y2={y + rowH - 1} stroke="var(--rule)" />
              {ln.spans.filter((sp) => sp.end >= t0).map((sp, k) => {
                const xa = x(sp.start), xb = x(sp.end), failed = sp.outcome === 'error', running = sp.outcome === 'running';
                return (
                  <rect key={k} className="span" style={{ '--i': Math.min(i * 4 + k, 24) } as React.CSSProperties} x={xa} y={y + 5} width={Math.max(2.5, xb - xa - 2)} height={16} rx={3}
                        fill={STAGE_COLOR[sp.stage] ?? 'var(--ink-3)'} stroke={failed ? 'var(--critical)' : undefined} strokeWidth={failed ? 2 : undefined} opacity={running ? 0.6 : 1}
                        onMouseMove={(e) => tip.show(<><b>{STAGE_NAME[sp.stage] ?? sp.stage}</b> on {sp.ticket}<br />{fmtMs((sp.end - sp.start) * 1000)}{running ? <><br />running now</> : null}{failed ? <><br />failed</> : null}</>, e)}
                        onMouseLeave={tip.hide} />
                );
              })}
            </g>
          );
        })}
        {live && <line x1={x(t1)} x2={x(t1)} y1={T} y2={H - 22} stroke="var(--ink)" strokeWidth={1} />}
      </svg>
    </div>
  );
}
