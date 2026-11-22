import { startTransition, useCallback, useEffect, useRef, useState } from 'react';
import type { JobState, RunListItem, Snapshot } from './types';

/** Streams telemetry snapshots from the local server (server-sent events) and keeps the run list fresh. */
export function useTelemetry() {
  const [runId, setRunIdState] = useState<string | null>(() => new URLSearchParams(location.search).get('run'));
  const setRunId = useCallback((id: string | null) => {
    setRunIdState(id);
    const url = new URL(location.href);
    if (id) url.searchParams.set('run', id); else url.searchParams.delete('run');
    history.replaceState(null, '', url);  // deep-linkable: the URL always reflects the selected run
  }, []);
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [job, setJob] = useState<JobState>({ status: 'idle' });
  const [runs, setRuns] = useState<RunListItem[]>([]);
  const [lost, setLost] = useState(false);
  const lastMsg = useRef(0);

  useEffect(() => {
    const es = new EventSource('/api/stream' + (runId ? `?run=${encodeURIComponent(runId)}` : ''));
    es.onmessage = (e) => {
      lastMsg.current = Date.now();
      try {
        const d = JSON.parse(e.data);
        startTransition(() => { setSnap(d.snapshot); setJob(d.job); });  // charts are non-urgent; keep input responsive
        setLost(false);
      } catch { /* ignore a malformed frame */ }
    };
    return () => es.close();
  }, [runId]);

  useEffect(() => {
    const t = setInterval(() => setLost(Date.now() - lastMsg.current > 4000), 1000);
    return () => clearInterval(t);
  }, []);

  const loadRuns = useCallback(async () => {
    try {
      const r = await fetch('/api/runs');
      setRuns((await r.json()).runs);
    } catch { /* the stream keeps working without the run list */ }
  }, []);
  useEffect(() => { loadRuns(); const t = setInterval(loadRuns, 6000); return () => clearInterval(t); }, [loadRuns]);

  return { snap, job, runs, runId, setRunId, lost, loadRuns };
}

/** Width of an element, kept current with a ResizeObserver, so charts draw at their real size. */
export function useWidth<T extends HTMLElement>(min = 240) {
  const ref = useRef<T | null>(null);
  const [width, setWidth] = useState(min);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(Math.max(min, Math.floor(el.clientWidth))));
    ro.observe(el); setWidth(Math.max(min, Math.floor(el.clientWidth)));
    return () => ro.disconnect();
  }, [min]);
  return [ref, width] as const;
}

/** Eases a number toward its new value (headline counters). Honours reduced motion. */
export function useCountUp(value: number, reduced: boolean, ms = 500): number {
  const [shown, setShown] = useState(value);
  const from = useRef(value);
  useEffect(() => {
    if (reduced) { setShown(value); from.current = value; return; }
    const start = performance.now(), a = from.current;
    let raf = 0;
    const step = (t: number) => {
      const k = Math.min(1, (t - start) / ms), v = a + (value - a) * (1 - Math.pow(1 - k, 3));
      setShown(v); from.current = v;
      if (k < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value, reduced, ms]);
  return shown;
}
