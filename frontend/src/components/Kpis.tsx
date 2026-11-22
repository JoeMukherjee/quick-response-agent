import type { CSSProperties } from 'react';
import { fmtClock, fmtMs, fmtN, reducedMotion } from '../format';
import { useCountUp } from '../hooks';
import type { Snapshot } from '../types';

function Count({ value, decimals = 0 }: { value: number; decimals?: number }) {
  const v = useCountUp(value, reducedMotion());
  return <>{decimals ? v.toFixed(decimals) : fmtN(Math.round(v))}</>;
}

export function Kpis({ snap }: { snap: Snapshot | null }) {
  const has = !!snap?.run;
  const pr = snap?.progress ?? { done: 0, total: 0, tickets_per_min: 0, outcomes: {}, actions: {} };
  const pct = pr.total ? (pr.done / pr.total) * 100 : 0;
  return (
    <section className="kpis">
      <div className="kpi" style={{ '--i': 0 } as CSSProperties}>
        <div className="label">Tickets handled</div>
        <div className="value num">{has ? <><Count value={pr.done} /><small>of {fmtN(pr.total)}</small></> : '0'}</div>
        <div className="sub">{has ? (snap!.run!.dry_run ? 'dry run, nothing was sent' : 'answers recorded on the helpdesk') : 'no run yet'}</div>
        <div className="bar" role="progressbar" aria-label="Tickets handled" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)}><div style={{ '--p': pct / 100 } as CSSProperties} /></div>
      </div>
      <div className="kpi" style={{ '--i': 1 } as CSSProperties}>
        <div className="label">Throughput</div>
        <div className="value num">{has ? <><Count value={pr.tickets_per_min} decimals={1} /><small>per minute</small></> : '0'}</div>
        <div className="sub">{has ? `${fmtClock(snap!.run!.elapsed)} elapsed` : ''}</div>
      </div>
      <div className="kpi" style={{ '--i': 2 } as CSSProperties}>
        <div className="label">Time per ticket</div>
        <div className="value num">{snap?.ticket_ms?.p50 ? <>{fmtMs(snap.ticket_ms.p50)}<small>median</small></> : 'n/a'}</div>
        <div className="sub">{snap?.ticket_ms?.p95 ? `95th percentile ${fmtMs(snap.ticket_ms.p95)}` : ''}</div>
      </div>
      <div className="kpi" style={{ '--i': 3 } as CSSProperties}>
        <div className="label">Workers busy</div>
        <div className="value num">{has ? <>{snap!.active_threads ?? 0}<small>of {snap!.run!.workers ?? '-'}</small></> : 'n/a'}</div>
        <div className="sub">{has ? 'parallel threads sharing one rate limit' : ''}</div>
      </div>
    </section>
  );
}
