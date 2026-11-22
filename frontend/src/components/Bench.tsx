import { useEffect, useRef, useState } from 'react';
import type { JobState } from '../types';
import { CachePanel } from './CachePanel';

type Kind = 'dry' | 'eval' | 'run' | 'final' | 'reset';
const OPTIONS: { kind: Kind; title: string; text: string; confirm?: string }[] = [
  { kind: 'dry', title: 'Dry Run', text: 'All stages on every open ticket, nothing is sent.' },
  { kind: 'eval', title: 'Evaluate', text: 'Score the labelled examples and our authored cases. No tickets are touched.' },
  { kind: 'run', title: 'Real Run', text: 'Answers the open tickets on the helpdesk. Type RUN to confirm.', confirm: 'RUN' },
  { kind: 'final', title: 'Final Run: Reset, Then Run Once', text: 'The submission run on Nordavo\'s server: re-opens all 30 practice tickets, then answers each one once, live. It replaces the answers now on the server. Type FINAL to confirm.', confirm: 'FINAL' },
  { kind: 'reset', title: 'Reset Practice Tickets', text: 'Re-opens all practice tickets and starts a new ledger epoch. Type RESET to confirm.', confirm: 'RESET' },
];

type Method = 'exhaustive' | 'cascade';
const METHODS: { id: Method; title: string; text: string }[] = [
  { id: 'exhaustive', title: 'Original: Score Every Entry', text: 'The judge scores every help-centre entry for every ticket. Perfect recall; cost grows with the knowledge base.' },
  { id: 'cascade', title: 'Cascade: Sharded Index Shortlist', text: 'A sharded keyword and vector index picks the top entries; the judge scores only those. Cost stays constant at any size.' },
];

const START_LABEL: Record<Kind, string> = { dry: 'Start Dry Run', eval: 'Run Evaluation', run: 'Start Real Run', final: 'Start Final Run', reset: 'Reset Tickets' };

export function Bench({ open, onClose, job, onStarted }: { open: boolean; onClose: () => void; job: JobState; onStarted: () => void }) {
  const dlg = useRef<HTMLDialogElement>(null);
  const [kind, setKind] = useState<Kind>('dry');
  const [workers, setWorkers] = useState(4);
  const [retrieval, setRetrieval] = useState<Method>('exhaustive');  // method for the next job; the original is the default
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
    const body: Record<string, unknown> = { workers, retrieval };
    if (kind === 'dry') Object.assign(body, { kind: 'run', dry_run: true });
    else if (kind === 'run') Object.assign(body, { kind: 'run', dry_run: false, confirm: confirm.trim() });
    else if (kind === 'final') Object.assign(body, { kind: 'final', dry_run: false, confirm: confirm.trim() });
    else if (kind === 'eval') body.kind = 'eval';
    else Object.assign(body, { kind: 'reset', confirm: confirm.trim() });
    setMsg('Starting…');
    try {
      const r = await fetch('/api/job', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-QRA': '1' }, body: JSON.stringify(body) });
      const d = await r.json();
      if (!r.ok) { setMsg(d.error ?? 'Could not start.'); return; }
      setMsg(`Started ${d.run_id}.`); setConfirm(''); onStarted();
      if (body.kind === 'run' || body.kind === 'final') onClose();
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
        <fieldset className="methodbox" disabled={kind === 'reset'}>
          <legend>Retrieval Method</legend>
          {METHODS.map((m) => (
            <label key={m.id} className="opt">
              <input type="radio" name="retrieval" checked={retrieval === m.id} onChange={() => setRetrieval(m.id)} />
              <b>{m.title}</b><small>{m.text}</small>
            </label>
          ))}
        </fieldset>
        {need && (
          <label className="field">Confirmation
            <input type="text" name="confirmation" value={confirm} placeholder={`Type ${need}…`} autoComplete="off" spellCheck={false} onChange={(e) => setConfirm(e.target.value)} />
          </label>
        )}
        <div className="msg" role="status" aria-live="polite">{job.status === 'running' ? `${job.kind === 'run' ? (job.dry_run ? 'Dry run' : 'Real run') : job.kind === 'final' ? 'Final run' : job.kind} in progress (${job.retrieval ?? 'exhaustive'} retrieval).` : msg}</div>
        <div className="metrics">
          {ev?.nordavo_examples && <div>Nordavo examples: action {ev.nordavo_examples.action}, unsafe replies {ev.nordavo_examples.unsafe_replies}, over-escalation {ev.nordavo_examples.over_escalation}</div>}
          {ev?.authored_cases && <div>Authored cases: action {ev.authored_cases.action}, unsafe replies {ev.authored_cases.unsafe_replies}</div>}
          {ev?.heldout_cases && <div>Held-out cases: action {ev.heldout_cases.action}, unsafe replies {ev.heldout_cases.unsafe_replies}</div>}
          {!ev && last?.message && <div>{last.message}</div>}
        </div>
        <CachePanel open={open} />
        <div className="row">
          <button className="btn" type="button" onClick={onClose}>Close</button>
          <button className="btn primary" type="button" disabled={job.status === 'running' || (!!need && confirm.trim() !== need)} onClick={start}>{job.status === 'running' ? 'Running…' : START_LABEL[kind]}</button>
        </div>
      </div>
    </dialog>
  );
}
