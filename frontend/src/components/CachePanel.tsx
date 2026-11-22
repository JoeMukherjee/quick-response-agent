import { useCallback, useEffect, useRef, useState } from 'react';
import { fmtN } from '../format';

interface Stat { entries: number; bytes: number; hits: number }
const STORES: { ns: string; label: string; note: string }[] = [
  { ns: 'decisions', label: 'Decisions', note: 'Decision 1 answers. Clearing costs tokens on the next run.' },
  { ns: 'drafts', label: 'Drafts', note: 'Flash replies and handoff notes. Clearing costs tokens on the next run.' },
];

const size = (b: number) => (b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(0, Math.round(b / 1024))} KB`);

/** Cache sizes and clear controls. A clear needs a second click, because it costs tokens to refill. */
export function CachePanel({ open }: { open: boolean }) {
  const [stats, setStats] = useState<Record<string, Stat>>({});
  const [armed, setArmed] = useState<string | null>(null);
  const [msg, setMsg] = useState('');
  const timer = useRef<number | undefined>(undefined);

  const load = useCallback(async () => {
    try { setStats((await (await fetch('/api/cache')).json()).namespaces ?? {}); } catch { /* the dialog still works without stats */ }
  }, []);
  useEffect(() => { if (open) load(); }, [open, load]);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const clear = async (ns: string) => {
    if (armed !== ns) {
      setArmed(ns); setMsg('');
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setArmed(null), 4000);
      return;
    }
    setArmed(null);
    try {
      const r = await fetch('/api/cache', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-QRA': '1' }, body: JSON.stringify({ namespace: ns }) });
      const d = await r.json();
      const n = Object.values<number>(d.removed ?? {}).reduce((s, v) => s + v, 0);
      setMsg(r.ok ? `Cleared ${fmtN(n)} ${n === 1 ? 'entry' : 'entries'}.` : d.error ?? 'Could not clear the cache.');
      load();
    } catch { setMsg('The server did not answer. The cache is unchanged; try again.'); }
  };

  const label = (ns: string, base: string) => (armed === ns ? 'Click Again to Confirm' : base);
  return (
    <fieldset className="cachebox">
      <legend>Response Cache</legend>
      {STORES.map((s) => {
        const v = stats[s.ns] ?? { entries: 0, bytes: 0, hits: 0 };
        return (
          <div className="cacherow" key={s.ns}>
            <div><b>{s.label}</b><small>{fmtN(v.entries)} {v.entries === 1 ? 'entry' : 'entries'}, {size(v.bytes)}, {fmtN(v.hits)} hits</small><small>{s.note}</small></div>
            <button type="button" className="btn" data-armed={armed === s.ns} onClick={() => clear(s.ns)}>{label(s.ns, `Clear ${s.label}`)}</button>
          </div>
        );
      })}
      <div className="cacherow">
        <div><small>Both stores are bounded: the least used entries are evicted first.</small></div>
        <button type="button" className="btn" data-armed={armed === 'all'} onClick={() => clear('all')}>{label('all', 'Clear All')}</button>
      </div>
      <div className="msg" role="status" aria-live="polite">{msg}</div>
    </fieldset>
  );
}
