import { fmtMs, fmtN } from '../format';
import type { Snapshot } from '../types';

/** Every chart has a table equivalent, so the numbers are reachable without hover, colour or a mouse. */
function Table({ caption, head, rows }: { caption: string; head: string[]; rows: (string | number)[][] }) {
  if (!rows.length) return null;
  return (
    <details className="tableview">
      <summary>Table view</summary>
      <table>
        <caption className="sr-only">{caption}</caption>
        <thead><tr>{head.map((h, i) => <th key={h} className={i ? 'r' : ''}>{h}</th>)}</tr></thead>
        <tbody>{rows.map((r, n) => <tr key={n}>{r.map((c, i) => <td key={i} className={i ? 'r num' : ''}>{c}</td>)}</tr>)}</tbody>
      </table>
    </details>
  );
}

export function LaneTable({ snap }: { snap: Snapshot | null }) {
  const rows = (snap?.lanes ?? []).map((l) => {
    const busy = l.spans.reduce((s, sp) => s + (sp.end - sp.start), 0);
    return [l.thread.replace('qra-worker-', 'worker '), l.spans.length, fmtMs(busy * 1000)];
  });
  return <Table caption="Work done by each worker thread" head={['Worker', 'Stages', 'Busy time']} rows={rows} />;
}

export function StageTable({ snap }: { snap: Snapshot | null }) {
  const rows = Object.entries(snap?.stages ?? {}).map(([k, s]) => [k, s.count, fmtMs(s.p50), fmtMs(s.p95), fmtMs(s.max)]);
  return <Table caption="Latency per stage" head={['Stage', 'Runs', 'Median', '95th', 'Max']} rows={rows} />;
}

export function RpsTable({ snap }: { snap: Snapshot | null }) {
  const rows = (snap?.http?.per_second ?? []).slice(-20).map((p) => [`${p.t} s`, p.n]);
  return <Table caption="Helpdesk requests per second, most recent 20 seconds" head={['At', 'Requests']} rows={rows} />;
}

export function TokenTable({ snap }: { snap: Snapshot | null }) {
  const rows = (snap?.tokens_per_minute?.series ?? []).map((s) => [`minute ${s.minute + 1}`, fmtN(s.tokens)]);
  return <Table caption="Decision tokens per minute" head={['Minute', 'Tokens']} rows={rows} />;
}

export function DecisionTable({ snap }: { snap: Snapshot | null }) {
  const a = snap?.progress?.actions ?? {}, total = Object.values(a).reduce((s, n) => s + n, 0);
  const rows = Object.entries(a).map(([k, n]) => [k, n, total ? `${Math.round((n / total) * 100)}%` : 'n/a']);
  return <Table caption="Tickets by action" head={['Action', 'Tickets', 'Share']} rows={rows} />;
}
