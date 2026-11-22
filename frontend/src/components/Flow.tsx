import type { CSSProperties } from 'react';
import { fmtMs, fmtN, reducedMotion } from '../format';
import type { Severity, Snapshot } from '../types';
import { ICONS } from './Glyphs';
import { useTooltip } from './Tooltip';

interface NodeDef { id: string; name: string; tool: string; icon: string; faults: string[] }
const judgeLabel = (provider?: string) => (provider === 'TypeSafe' ? 'TypeSafe Jev' : provider === 'Gemini' || !provider ? 'Gemini Decision 1' : provider);

const NODES: NodeDef[] = [
  { id: 'helpdesk', name: 'Helpdesk', tool: 'Roosterly API', icon: 'helpdesk', faults: ['http_503', 'http_429', 'transport_error', 'list_failed', 'http_401', 'http_404'] },
  { id: 'queue', name: 'Queue', tool: 'bounded, in-process', icon: 'queue', faults: ['stale_epoch', 'lock_held'] },
  { id: 'workers', name: 'Workers', tool: 'thread pool', icon: 'workers', faults: ['ticket_error', 'service_error'] },
  { id: 'redact', name: 'Redact', tool: 'IBAN and card', icon: 'redact', faults: ['payment_data_redacted'] },
  { id: 'judge', name: 'Judge', tool: 'Gemini Decision 1', icon: 'judge', faults: ['judge_transient', 'judge_rejected', 'judge_safety', 'fatal_auth'] },
  { id: 'policy', name: 'Policy', tool: 'truth table in code', icon: 'policy', faults: [] },
  { id: 'draft', name: 'Draft + verify', tool: 'Gemini 3.8 Flash, Agno', icon: 'draft', faults: ['draft_error', 'verify_reject'] },
  { id: 'ledger', name: 'Ledger', tool: 'SQLite, WAL', icon: 'ledger', faults: [] },
  { id: 'deliver', name: 'Deliver', tool: 'exactly once', icon: 'deliver', faults: ['http_409', 'unknown_outcome', 'http_422', 'http_400'] },
];
const CARD = { w: 116, h: 150, y: 44, step: 140, x0: 2 };
const nx = (i: number) => CARD.x0 + i * CARD.step;
const RANK: Record<Severity, number> = { info: 0, warn: 1, error: 2, fatal: 3 };
const COLOR: Record<Severity, string> = { info: 'var(--info-ink)', warn: 'var(--warn)', error: 'var(--serious)', fatal: 'var(--critical)' };
const SHAPE: Record<Severity, string> = { info: 'M0-10a10 10 0 1 0 .01 0z', warn: 'M0-11 11 9H-11z', error: 'M-9-9h18v18h-18z', fatal: 'M0-12 12 0 0 12-12 0z' };

/** Dots travel along an edge while something is really happening there: SVG-native motion, no timers. */
function Dots({ pathId, on, speed, count = 3, r = 3 }: { pathId: string; on: boolean; speed: number; count?: number; r?: number }) {
  if (!on) return null;
  const dur = Math.max(0.9, 2.6 / speed);
  return (
    <>
      {Array.from({ length: count }, (_, k) => (
        <circle key={k} r={r} className="edge-dot">
          <animateMotion dur={`${dur}s`} begin={`${-(k * dur) / count}s`} repeatCount="indefinite" keyPoints="0;1" keyTimes="0;1" calcMode="linear">
            <mpath href={`#${pathId}`} />
          </animateMotion>
          <animate attributeName="opacity" values="0;1;1;0" keyTimes="0;.15;.85;1" dur={`${dur}s`} begin={`${-(k * dur) / count}s`} repeatCount="indefinite" />
        </circle>
      ))}
    </>
  );
}

export function Flow({ snap }: { snap: Snapshot | null }) {
  const tip = useTooltip();
  const reduced = reducedMotion();
  const has = !!snap?.run, running = snap?.run?.status === 'running';
  const g = (cls: string) => snap?.faults?.find((f) => f.class === cls)?.count ?? 0;
  const total = snap?.run?.total ?? 0, done = snap?.progress?.done ?? 0;
  const m = snap?.models ?? { judge: { calls: 0, cached: 0, tokens: 0, p50: 0, p95: 0 }, draft: { calls: 0, cached: 0, tokens: 0, p50: 0, p95: 0 }, note: { calls: 0, cached: 0, tokens: 0, p50: 0, p95: 0 } };
  const a = snap?.progress?.actions ?? {}, o = snap?.progress?.outcomes ?? {}, act = snap?.activity ?? {};
  const threads = snap?.active_threads ?? 0, workers = snap?.run?.workers ?? 0;

  const metrics: [string, string][] = [
    [`${fmtN(total)} tickets`, `${fmtN(snap?.http?.calls)} API calls`],
    [`${running ? Math.max(0, Math.min(workers, total - done)) : 0} in flight`, `${fmtN(done)} of ${fmtN(total)} done`],
    [`${threads} of ${workers}`, 'threads busy'],
    [`${fmtN(g('payment_data_redacted'))} redacted`, 'before any model'],
    [`${fmtN(m.judge.calls + m.judge.cached)} calls`, `${snap?.progress?.resampled ? `${snap.progress.resampled} resampled` : `median ${fmtMs(m.judge.p50)}`}`],
    [`${a.reply ?? 0} / ${a.handoff ?? 0} / ${a.close ?? 0}`, 'reply / handoff / close'],
    [`${fmtN(m.draft.calls + m.draft.cached + m.note.calls + m.note.cached)} texts`, `${g('verify_reject')} rejected`],
    [`${fmtN((o.sent ?? 0) + (o.reconciled ?? 0) + (o['dry-run'] ?? 0))} recorded`, 'decided, then sent'],
    [`${fmtN(o.sent ?? 0)} sent`, `${o.reconciled ?? 0} reconciled`],
  ];
  const active: boolean[] = [
    (act.helpdesk ?? 0) > 0, running && done < total, threads > 0, (act.plan ?? 0) > 0, (act.judge ?? 0) > 0, (act.plan ?? 0) > 0,
    (act.draft ?? 0) + (act.verify ?? 0) + (act.note ?? 0) > 0, (act.deliver ?? 0) > 0, (act.deliver ?? 0) > 0 || (act.helpdesk ?? 0) > 0,
  ];
  const live = running && !reduced;
  const intensity = Math.min(3, 1 + (threads || 1) / 2);

  const cy = CARD.y + CARD.h + 36, by = cy + 62;
  const x0c = nx(0) + CARD.w / 2, x8c = nx(8) + CARD.w / 2;
  const peak = Math.max(0, ...(snap?.http?.per_second ?? [{ n: 0 }]).map((p) => p.n));
  const tpmNow = snap?.tokens_per_minute?.series[(snap?.tokens_per_minute?.series.length ?? 1) - 1]?.tokens ?? 0;
  const hits = m.judge.cached + m.draft.cached + m.note.cached, calls = m.judge.calls + m.draft.calls + m.note.calls;
  const chips = [
    { id: 'limiter', name: 'Rate limiter', sub: `peak ${peak}/s of ${snap?.http?.limit_rps ?? 5}/s`, x: nx(0), ties: [0, 8] },
    { id: 'budget', name: 'Token budget', sub: snap?.tokens_per_minute?.limit === null ? `${fmtN(tpmNow)} this minute, no window` : `${fmtN(tpmNow)} of ${fmtN(snap?.tokens_per_minute?.limit ?? 50000)} this minute`, x: nx(4) - 40, ties: [4] },
    { id: 'cache', name: 'Response cache', sub: `${hits} hits, ${calls} live calls`, x: nx(6) - 24, ties: [4, 6] },
  ];

  return (
    <svg id="flow" viewBox="0 0 1240 372" role="img" aria-label="Data flow from the helpdesk through the agent and back">
      <defs>
        <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M1 1l6 3-6 3z" fill="var(--rule-strong)" /></marker>
      </defs>

      {/* answers return to the helpdesk */}
      <path id="p-return" className="edge" d={`M${x8c} ${CARD.y} C ${x8c} -12, ${x0c} -12, ${x0c} ${CARD.y}`} markerEnd="url(#arrow)" />
      <text x={(x8c + x0c) / 2} y={12} textAnchor="middle" className="vsub">answers go back to the helpdesk</text>
      <Dots pathId="p-return" on={live && active[8]} speed={intensity} count={4} />

      {NODES.slice(0, -1).map((_, i) => {
        const y = CARD.y + 34;
        return (
          <g key={i}>
            <path id={`p-edge-${i}`} className="edge draw" pathLength={1} style={{ '--i': i } as CSSProperties} d={`M${nx(i) + CARD.w} ${y} L${nx(i + 1) - 2} ${y}`} markerEnd="url(#arrow)" />
            <Dots pathId={`p-edge-${i}`} on={live && (active[i] || active[i + 1])} speed={intensity} />
          </g>
        );
      })}

      {NODES.map((base, i) => {
        const n = base.id === 'judge' ? { ...base, tool: judgeLabel(snap?.run?.judge?.provider) } : base;
        const x = nx(i), cx = x + CARD.w / 2;
        const rows = (snap?.faults ?? []).filter((f) => n.faults.includes(f.class) && f.count > 0);
        const bad = rows.reduce((s, f) => s + f.count, 0);
        const worst: Severity = rows.reduce<Severity>((w, f) => (RANK[f.severity] > RANK[w] ? f.severity : w), 'info');
        return (
          <g key={n.id} data-id={n.id} style={{ '--i': i } as CSSProperties} className={`node reveal-pop${active[i] && !reduced ? ' active' : ''}`}
             onMouseMove={(e) => tip.show(<><b>{n.name}</b><br />{n.tool}</>, e)} onMouseLeave={tip.hide}>
            <rect className="hit" x={x} y={CARD.y - 6} width={CARD.w} height={CARD.h + 12} fill="transparent" />
            <g className="node-body">
            <rect className="card" x={x} y={CARD.y} width={CARD.w} height={CARD.h} rx={10} />
            <clipPath id={`clip-${n.id}`}><rect x={x} y={CARD.y} width={CARD.w} height={CARD.h} rx={10} /></clipPath>
            <g clipPath={`url(#clip-${n.id})`}><rect className="scan" x={x} y={CARD.y} width={40} height={3} /></g>
            <g transform={`translate(${cx - 12} ${CARD.y + 22})`}><g className="icon-wrap"><path className="icon" d={ICONS[n.icon]} /></g></g>
            <text className="name" x={cx} y={CARD.y + 76} textAnchor="middle">{n.name}</text>
            <text className="tool" x={cx} y={CARD.y + 91} textAnchor="middle">{n.tool}</text>
            <text className="m1 num" x={cx} y={CARD.y + 118} textAnchor="middle">{has ? metrics[i][0] : 'n/a'}</text>
            <text className="m2 num" x={cx} y={CARD.y + 135} textAnchor="middle">{has ? metrics[i][1] : ''}</text>
            {bad > 0 && (
              <g className="badge" transform={`translate(${x + CARD.w - 6} ${CARD.y + 4})`}
                 onMouseMove={(e) => { e.stopPropagation(); tip.show(<>{rows.map((f) => <div key={f.class}>{f.label}: {f.count}</div>)}</>, e); }}
                 onMouseLeave={tip.hide}>
                <path d={SHAPE[worst]} fill={COLOR[worst]} />
                <text x={0} y={3.4} textAnchor="middle" fill={worst === 'warn' ? '#1a1a19' : '#fff'}>{bad > 99 ? '99+' : bad}</text>
              </g>
            )}
            </g>
          </g>
        );
      })}

      {chips.map((c) => (
        <g key={c.id} className="chip">
          {c.ties.map((t) => {
            const tx = nx(t) + CARD.w / 2;
            return <path key={t} className="tie" d={`M${tx} ${CARD.y + CARD.h} L${tx} ${CARD.y + CARD.h + 14} L${c.x + 84} ${CARD.y + CARD.h + 14} L${c.x + 84} ${cy}`} />;
          })}
          <rect x={c.x} y={cy} width={168} height={40} rx={8} />
          <text className="cname" x={c.x + 12} y={cy + 17}>{c.name}</text>
          <text x={c.x + 12} y={cy + 32}>{c.sub}</text>
        </g>
      ))}

      <path id="p-bus" className="tie" d={`M${x0c} ${by} L${x8c} ${by}`} />
      <text x={nx(0)} y={by + 18} className="vsub">Telemetry: every stage, call and failure is an event</text>
      <Dots pathId="p-bus" on={live && active.some(Boolean)} speed={1} count={5} r={2.6} />
    </svg>
  );
}
