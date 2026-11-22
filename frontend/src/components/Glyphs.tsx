import type { Severity } from '../types';

/** Severity is always a shape AND a label, never colour alone. */
export function SeverityGlyph({ sev }: { sev: Severity | 'ok' }) {
  const common = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.4, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const };
  return (
    <svg viewBox="0 0 14 14" aria-hidden="true">
      {sev === 'info' && (<><circle cx="7" cy="7" r="5.5" {...common} /><path d="M7 6.3v3.4M7 4.3v.1" {...common} strokeWidth={1.5} /></>)}
      {sev === 'warn' && (<><path d="M7 1.8 12.8 12H1.2z" {...common} /><path d="M7 5.6v3M7 10.4v.1" {...common} strokeWidth={1.5} /></>)}
      {sev === 'error' && (<><rect x="1.8" y="1.8" width="10.4" height="10.4" rx="2" {...common} /><path d="M4.8 4.8l4.4 4.4M9.2 4.8 4.8 9.2" {...common} strokeWidth={1.5} /></>)}
      {sev === 'fatal' && (<><path d="M7 1.2 12.8 7 7 12.8 1.2 7z" {...common} /><path d="M7 4.4v3.4M7 9.6v.1" {...common} strokeWidth={1.5} /></>)}
      {sev === 'ok' && (<><circle cx="7" cy="7" r="5.5" {...common} /><path d="M4.6 7.2 6.4 9l3-3.4" {...common} strokeWidth={1.5} /></>)}
    </svg>
  );
}

/** Original monoline marks for each pipeline component (24px grid). */
export const ICONS: Record<string, string> = {
  helpdesk: 'M3 13h5l1.5 3h5L16 13h5M3 13l2.5-8h13L21 13v6H3z',
  queue: 'M4 5h16v3.5H4zM4 10.5h16V14H4zM4 16h16v3H4z',
  workers: 'M3 6h9M3 10h14M3 14h7M3 18h16M15 6h.1M19 10h.1M12 14h.1M21 18h.1',
  redact: 'M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6zM8.5 12h7',
  judge: 'M12 3v6M12 9l-6 6v3M12 9l6 6v3M12 3h.1M6 18h.1M18 18h.1',
  policy: 'M4 5h16v14H4zM4 10h16M4 15h16M10 5v14',
  draft: 'M4 20l4-1 10.5-10.5-3-3L5 16zM13.5 7.5l3 3M15 19l2 2 4-4.5',
  ledger: 'M5 6c0-1.7 3.1-3 7-3s7 1.3 7 3-3.1 3-7 3-7-1.3-7-3zM5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3',
  deliver: 'M3 11l18-8-8 18-2-8zM11 13l4-4',
};

export function BrandMark() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 5h16v11H11l-4 4v-4H4z" /><path d="M8.5 10.5h6M12.5 8l2.5 2.5-2.5 2.5" />
    </svg>
  );
}
