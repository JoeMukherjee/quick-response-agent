import { useEffect, useRef, useState } from 'react';
import type { JobState } from '../types';

type Kind = 'dry' | 'eval' | 'run' | 'reset';
const OPTIONS: { kind: Kind; title: string; text: string; confirm?: string }[] = [
  { kind: 'dry', title: 'Dry Run', text: 'All stages on every open ticket, nothing is sent.' },
  { kind: 'eval', title: 'Evaluate', text: 'Score the labelled examples and our authored cases. No tickets are touched.' },
  { kind: 'run', title: 'Real Run', text: 'Answers tickets on the helpdesk. Type RUN to confirm.', confirm: 'RUN' },
  { kind: 'reset', title: 'Reset Practice Tickets', text: 'Re-opens all practice tickets and starts a new ledger epoch. Type RESET to confirm.', confirm: 'RESET' },
];

const START_LABEL: Record<Kind, string> = { dry: 'Start Dry Run', eval: 'Run Evaluation', run: 'Start Real Run', reset: 'Reset Tickets' };

export function Bench({ open, onClose, job, onStarted }: { open: boolean; onClose: () => void; job: JobState; onStarted: () => void }) {
  const dlg = useRef<HTMLDialogElement>(null);
  const [kind, setKind] = useState<Kind>('dry');
  const [workers, setWorkers] = useState(4);
  const [confirm, setConfirm] = useState('');
  const [msg, setMsg] = useState('');
  const need = OPTIONS.find((o) => o.kind === kind)?.confirm;

  useEffect(() => {
    const d = dlg.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);

  const start = async () => {
    const body: Record<string, unknown> = { workers };
    if (kind === 'dry') Object.assign(body, { kind: 'run', dry_run: true });
    else if (kind === 'run') Object.assign(body, { kind: 'run', dry_run: false, confirm: confirm.trim() });
    else if (kind === 'eval') body.kind = 'eval';
    else Object.assign(body, { kind: 'reset', confirm: confirm.trim() });
    setMsg('Starting…');
    try {
      const r = await fetch('/api/job', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-QRA': '1' }, body: JSON.stringify(body) });
      const d = await r.json();
      if (!r.ok) { setMsg(d.error ?? 'Could not start.'); return; }
      setMsg(`Started ${d.run_id}.`); setConfirm(''); onStarted();
      if (body.kind === 'run') onClose();
    } catch { setMsg('The server did not answer. It is still running; try again.'); }
  };

  const last = job.last;
  const ev = last?.kind === 'eval' ? last.summary : null;
  return (
    <dialog ref={dlg} onClose={onClose}>
      <div className="dlg">
        <h2>Bench</h2>
        <p className="muted" style={{ fontSize: 12.5 }}>Runs here use the same code as <span className="mono">python -m qra</span>. A dry run decides and drafts but posts nothing.</p>
        {OPTIONS.map((o) => (
          <label key={o.kind} className="opt">
            <input type="radio" name="job-kind" checked={kind === o.kind} onChange={() => { setKind(o.kind); setConfirm(''); }} />
            <b>{o.title}</b><small>{o.text}</small>
          </label>
        ))}
        <label className="field">Workers (1 to 8)
          <input type="number" name="workers" inputMode="numeric" autoComplete="off" min={1} max={8} value={workers} disabled={kind === 'eval' || kind === 'reset'} onChange={(e) => setWorkers(Number(e.target.value) || 1)} />
        </label>
        {need && (
          <label className="field">Confirmation
            <input type="text" name="confirmation" value={confirm} placeholder={`Type ${need}…`} autoComplete="off" spellCheck={false} onChange={(e) => setConfirm(e.target.value)} />
          </label>
        )}
        <div className="msg" role="status" aria-live="polite">{job.status === 'running' ? `${job.kind === 'run' ? (job.dry_run ? 'Dry run' : 'Real run') : job.kind} in progress.` : msg}</div>
        <div className="metrics">
          {ev?.nordavo_examples && <div>Nordavo examples: action {ev.nordavo_examples.action}, unsafe replies {ev.nordavo_examples.unsafe_replies}, over-escalation {ev.nordavo_examples.over_escalation}</div>}
          {ev?.authored_cases && <div>Authored cases: action {ev.authored_cases.action}, unsafe replies {ev.authored_cases.unsafe_replies}</div>}
          {!ev && last?.message && <div>{last.message}</div>}
        </div>
        <div className="row">
          <button className="btn" type="button" onClick={onClose}>Close</button>
          <button className="btn primary" type="button" disabled={job.status === 'running' || (!!need && confirm.trim() !== need)} onClick={start}>{job.status === 'running' ? 'Running…' : START_LABEL[kind]}</button>
        </div>
      </div>
    </dialog>
  );
}
