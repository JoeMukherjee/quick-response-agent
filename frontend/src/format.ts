export const fmtMs = (ms: number | null | undefined): string =>
  ms == null ? 'n/a' : ms >= 1000 ? `${(ms / 1000).toFixed(ms >= 10000 ? 0 : 1)}\u00A0s` : `${Math.round(ms)}\u00A0ms`;

export const fmtClock = (s: number): string => {
  const t = Math.max(0, Math.round(s));
  return `${String(Math.floor(t / 60)).padStart(2, '0')}:${String(t % 60).padStart(2, '0')}`;
};

export const fmtN = (n: number | null | undefined): string => (n ?? 0).toLocaleString('en');

export function niceTicks(a: number, b: number, n: number): number[] {
  const span = Math.max(1e-6, b - a), raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const out: number[] = [];
  for (let t = Math.ceil(a / step) * step; t <= b + 1e-9; t += step) out.push(t);
  return out;
}

export const reducedMotion = (): boolean =>
  (typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches) ||
  (typeof document !== 'undefined' && document.documentElement.dataset.motion === 'off');

export const store = {
  get(k: string): string | null { try { return localStorage.getItem(k); } catch { return null; } },
  set(k: string, v: string): void { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};

/** Fixed-order categorical hues for pipeline stages (validated palette; colour follows the stage, never its rank). */
export const STAGES: { key: string; name: string; color: string }[] = [
  { key: 'plan', name: 'Plan', color: 'var(--s1)' },
  { key: 'draft', name: 'Draft', color: 'var(--s2)' },
  { key: 'verify', name: 'Verify', color: 'var(--s3)' },
  { key: 'note', name: 'Note', color: 'var(--s4)' },
  { key: 'deliver', name: 'Deliver', color: 'var(--s5)' },
];
export const STAGE_COLOR: Record<string, string> = Object.fromEntries(STAGES.map((s) => [s.key, s.color]));
export const STAGE_NAME: Record<string, string> = Object.fromEntries(STAGES.map((s) => [s.key, s.name]));
