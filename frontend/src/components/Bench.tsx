import { useEffect, useRef, useState } from 'react';
import type { JobState } from '../types';
import { CachePanel } from './CachePanel';

type Kind = 'nordavo' | 'ours' | 'live';
const OPTIONS: { kind: Kind; title: string; text: string; confirm?: string }[] = [
  { kind: 'nordavo', title: "Nordavo's labelled examples", text: "Calls Nordavo's API now (GET /examples) and scores the agent on the 10 example tickets, with their expected answers. Nothing is sent to anyone." },
  { kind: 'ours', title: 'Our own test cases', text: 'Scores the 26 cases we wrote, and Nordavo's 30 practice tickets (fetched live from their API) against labels we wrote from the client rules. Nothing is sent to anyone.' },
  { kind: 'live', title: "Nordavo's helpdesk (live API)", text: "The submission run. Calls Nordavo's API live: re-opens all 30 practice tickets and answers each one once. It replaces the answers there now. Type FINAL to confirm.", confirm: 'FINAL' },
];

type Judge = 'gemini' | 'typesafe';
const JUDGES: { id: Judge; title: string; text: string }[] = [
  { id: 'typesafe', title: 'TypeSafe Jev 1.14 (recommended)', text: "Faster (about 3x on a full run) and cheaper ($0.042 per million input tokens, about $0.08 per 100 tickets), with no per-minute token cap. On Nordavo's labelled examples it chose the right action 7 times in 10 against 10 for Gemini; every miss goes to a person, never an unsafe reply. Needs TYPESAFE_API_KEY in .env." },
  { id: 'gemini', title: 'Gemini Decision 1', text: "More accurate on Nordavo's labelled examples (10 of 10 actions) at $0.10 per million input tokens (about $0.11 per 100 tickets), but limited to about 50k tokens a minute on the free tier, which makes the end of a run wait." },
];

type Method = 'exhaustive' | 'cascade';
const METHODS: { id: Method; title: string; text: string }[] = [
  { id: 'exhaustive', title: 'Original: Score Every Entry', text: 'The judge scores every help-centre entry for every ticket. Perfect recall; cost grows with the knowledge base.' },
  { id: 'cascade', title: 'Cascade: Sharded Index Shortlist', text: 'A sharded keyword and vector index picks the top entries; the judge scores only those. Cost stays constant at any size.' },
];

const START_LABEL: Record<Kind, string> = { nordavo: "Score Nordavo's Examples", ours: 'Score Our Test Cases', live: "Run on Nordavo's Helpdesk" };

export function Bench({ open, onClose, job, onStarted }: { open: boolean; onClose: () => void; job: JobState; onStarted: () => void }) {
  const dlg = useRef<HTMLDialogElement>(null);
  const [kind, setKind] = useState<Kind>('nordavo');
  const [workersText, setWorkersText] = useState('4');  // text, so the box can be emptied and retyped; clamped to 1-8 when a job starts
  const [judge, setJudge] = useState<Judge>('typesafe');  // decision model for the next job; replaced by the server's configured default when the dialog opens
  const [retrieval, setRetrieval] = useState<Method>('exhaustive');  // method for the next job; the original is the default
  const [confirm, setConfirm] = useState('');
  const [apiUrl, setApiUrl] = useState('');  // empty: the saved ticket API; the key is typed per job, kept in memory only, never stored
  const [apiKey, setApiKey] = useState('');
  const [defaultApi, setDefaultApi] = useState('');
  const [msg, setMsg] = useState('');
  const need = OPTIONS.find((o) => o.kind === kind)?.confirm;

  useEffect(() => {
    const d = dlg.current;
    if (!d) return;
    if (open && !d.open) { d.showModal(); fetch('/api/health').then((r) => r.json()).then((h) => { setDefaultApi(h.ticket_api ?? ''); if (h.judge_default === 'gemini' || h.judge_default === 'typesafe') setJudge(h.judge_default); }).catch(() => {}); }
    if (!open && d.open) d.close();
  }, [open]);

  const start = async () => {
    const workers = Math.min(8, Math.max(1, Math.floor(Number(workersText)) || 4));
    const body: Record<string, unknown> = { workers, retrieval, judge };
    if (kind === 'live' && (apiUrl.trim() || apiKey.trim())) body.api = { url: apiUrl.trim(), key: apiKey.trim() };
    if (kind === 'live') Object.assign(body, { kind: 'final', dry_run: false, confirm: confirm.trim() });
    else Object.assign(body, { kind: 'eval', suite: kind });
    setMsg('Starting…');
    try {
      const r = await fetch('/api/job', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-QRA': '1' }, body: JSON.stringify(body) });
      const d = await r.json();
      if (!r.ok) { setMsg(d.error ?? 'Could not start.'); return; }
      setMsg(`Started ${d.run_id}.`); setConfirm(''); onStarted();
      if (body.kind === 'final') onClose();
    } catch { setMsg('The server did not answer. It is still running; try again.'); }
  };

  const last = job.last;
  const ev = last?.kind === 'eval' ? last.summary : null;
  return (
    <dialog ref={dlg} onClose={onClose}>
      <div className="dlg">
        <h2>Bench</h2>
        <p className="muted" style={{ fontSize: 12.5 }}>Runs here use the same code as <span className="mono">python -m qra</span>. Only the last option sends anything to Nordavo.</p>
        {OPTIONS.map((o) => (
          <label key={o.kind} className="opt">
            <input type="radio" name="job-kind" checked={kind === o.kind} onChange={() => { setKind(o.kind); setConfirm(''); }} />
            <b>{o.title}</b><small>{o.text}</small>
          </label>
        ))}
        {kind === 'live' && <fieldset className="methodbox">
          <legend>Ticket API</legend>
          <label className="field">Address
            <input type="text" name="ticket-api-url" autoComplete="off" spellCheck={false} value={apiUrl} placeholder={defaultApi || 'https://…'} onChange={(e) => setApiUrl(e.target.value)} />
          </label>
          <label className="field">API key (only needed for another address)
            <input type="password" name="ticket-api-key" autoComplete="off" value={apiKey} placeholder="saved key is used for the saved address" onChange={(e) => setApiKey(e.target.value)} />
          </label>
          <small className="muted">Leave empty to use the saved Nordavo API. The saved key is never sent to a different address.</small>
        </fieldset>}
        <label className="field">Workers (1 to 8)
          <input type="number" name="workers" inputMode="numeric" autoComplete="off" min={1} max={8} value={workersText} disabled={kind !== 'live'} onChange={(e) => setWorkersText(e.target.value)} onBlur={() => setWorkersText(String(Math.min(8, Math.max(1, Math.floor(Number(workersText)) || 4))))} />
        </label>
        <fieldset className="methodbox">
          <legend>Decision Model</legend>
          {JUDGES.map((m) => (
            <label key={m.id} className="opt">
              <input type="radio" name="judge" checked={judge === m.id} onChange={() => setJudge(m.id)} />
              <b>{m.title}</b><small>{m.text}</small>
            </label>
          ))}
        </fieldset>
        <fieldset className="methodbox">
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
        <div className="msg" role="status" aria-live="polite">{job.status === 'running' ? `${job.kind === 'run' ? (job.dry_run ? 'Dry run' : 'Real run') : job.kind === 'final' ? 'Final run' : job.kind} in progress (${job.judge === 'typesafe' ? 'TypeSafe Jev' : 'Gemini Decision 1'}, ${job.retrieval ?? 'exhaustive'} retrieval).` : msg}</div>
        <div className="metrics">
          {ev?.sources?.nordavo_examples && <div>Called Nordavo's API live: {ev.sources.nordavo_examples.call} at {ev.sources.nordavo_examples.at}, {ev.sources.nordavo_examples.tickets} tickets returned.</div>}
          {ev?.nordavo_examples && <div>Nordavo examples: action {ev.nordavo_examples.action}, unsafe replies {ev.nordavo_examples.unsafe_replies}, over-escalation {ev.nordavo_examples.over_escalation}</div>}
          {ev?.practice_labels && <div>Practice tickets (our labels): action {ev.practice_labels.action}, priority {ev.practice_labels.priority}, unsafe replies {ev.practice_labels.unsafe_replies}</div>}
          {ev?.authored_cases && <div>Authored cases: action {ev.authored_cases.action}, unsafe replies {ev.authored_cases.unsafe_replies}</div>}
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
