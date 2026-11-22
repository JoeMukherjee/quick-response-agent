import { Fragment, ViewTransition } from 'react';
import { fmtClock, fmtMs } from '../format';
import type { Severity, Snapshot } from '../types';
import { SeverityGlyph } from './Glyphs';

const RANK: Record<Severity, number> = { info: 0, warn: 1, error: 2, fatal: 3 };

/**
 * Every known failure class, zero included, so "did not happen" is never confused with "not monitored".
 * Rows carry their identity (the class), so when a failure occurs the row slides up to where it belongs.
 */
export function FaultTable({ snap }: { snap: Snapshot | null }) {
  const faults = snap?.faults ?? [];
  if (!faults.length) return <div className="empty">Failure classes appear once a run exists.</div>;
  const rows = [...faults].sort((a, b) => Number(b.count > 0) - Number(a.count > 0) || RANK[b.severity] - RANK[a.severity]);
  return (
    <table>
      <thead><tr><th>Failure</th><th>Where</th><th className="r">Count</th><th>How it is handled</th></tr></thead>
      <tbody>
        {rows.map((f, n) => (
          <ViewTransition key={f.class} default="row">
            <tr className={f.count ? '' : 'zero'} style={{ '--i': Math.min(n, 14) } as React.CSSProperties}>
              <td><span className="sev" data-s={f.count ? f.severity : 'ok'}><SeverityGlyph sev={f.count ? f.severity : 'ok'} />{f.label}</span></td>
              <td className="muted">{f.layer}</td>
              <td className="r num">{f.count || '0'}</td>
              <td className={f.count ? '' : 'muted'}>{f.handling}</td>
            </tr>
          </ViewTransition>
        ))}
      </tbody>
    </table>
  );
}

/** Ids and decisions only: ticket text is never stored, so it can never be shown. New tickets fade in. */
export function RecentTickets({ snap }: { snap: Snapshot | null }) {
  const rows = snap?.recent ?? [];
  if (!rows.length) return <div className="empty">No tickets yet.</div>;
  return (
    <table>
      <thead><tr><th>At</th><th>Ticket</th><th>Action</th><th>Priority</th><th>Language</th><th className="r">Row</th><th className="r">Confidence</th><th className="r">Time</th><th>Outcome</th></tr></thead>
      <tbody>
        {rows.map((r, n) => (
          <Fragment key={r.ticket}>
            <tr style={{ '--i': Math.min(n, 12) } as React.CSSProperties}>
              <td className="num muted">{fmtClock(r.t)}</td>
              <td className="mono" translate="no">{r.ticket}</td>
              <td>{r.action ? <span className={`tag ${r.action}`}>{r.action}</span> : <span className="muted">n/a</span>}</td>
              <td>{r.priority ?? 'n/a'}</td>
              <td>{r.language ?? 'n/a'}</td>
              <td className="r num">{r.row ?? 'n/a'}</td>
              <td className="r num">{r.confidence != null ? r.confidence.toFixed(2) : 'n/a'}</td>
              <td className="r num">{fmtMs(r.ms)}</td>
              <td>{r.outcome}{r.error ? <span className="muted"> {r.error}</span> : null}</td>
            </tr>
          </Fragment>
        ))}
      </tbody>
    </table>
  );
}
