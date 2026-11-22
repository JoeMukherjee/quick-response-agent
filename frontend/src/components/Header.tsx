import { fmtClock, store } from '../format';
import type { JobState, RunListItem, Snapshot } from '../types';
import { BrandMark } from './Glyphs';

interface Props {
  snap: Snapshot | null; job: JobState; runs: RunListItem[]; runId: string | null; lost: boolean;
  onRun: (id: string | null) => void; onBench: () => void;
}

function statusOf(snap: Snapshot | null, job: JobState): { state: string; text: string } {
  if (job.status === 'running' && job.kind !== 'run') return { state: 'running', text: job.kind === 'eval' ? 'Evaluating' : 'Resetting' };
  if (!snap?.run) return { state: 'idle', text: 'No run yet' };
  const s = snap.run.status, text = s.charAt(0).toUpperCase() + s.slice(1) + (snap.run.dry_run ? ', dry run' : '');
  const state = s === 'running' ? 'running' : s.startsWith('stopped') ? 'fatal' : s.includes('errors') ? 'error' : 'finished';
  return { state, text };
}

function toggleTheme() {
  const root = document.documentElement;
  const dark = (root.dataset.theme ?? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark';
  root.dataset.theme = dark ? 'light' : 'dark';
  store.set('qra-theme', root.dataset.theme);
}

export function Header({ snap, job, runs, runId, lost, onRun, onBench }: Props) {
  const st = statusOf(snap, job);
  return (
    <header className="top">
      <h1 className="brand"><BrandMark /><span>Quick Response Agent</span></h1>
      <select aria-label="Run" value={runId ?? ''} onChange={(e) => onRun(e.target.value || null)}>
        <option value="">Latest run</option>
        {runs.map((r) => <option key={r.run_id} value={r.run_id}>{r.run_id} ({r.tickets} tickets, {r.faults} faults)</option>)}
      </select>
      <span className="status" data-state={st.state} role="status" aria-live="polite"><i aria-hidden="true" /><span>{st.text}</span></span>
      <span className="num muted">{snap?.run ? fmtClock(snap.run.elapsed) : ''}</span>
      <span className="spacer" />
      <span className="conn" data-lost={lost ? 1 : 0} aria-live="polite">{lost ? 'Connection lost, retrying…' : 'Live'}</span>
      <button className="btn" onClick={toggleTheme} aria-label="Toggle light or dark theme">Theme</button>
      <button className="btn primary" onClick={onBench}>Bench</button>
    </header>
  );
}
