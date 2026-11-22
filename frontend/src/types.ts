export type Severity = 'info' | 'warn' | 'error' | 'fatal';

export interface Span { stage: string; ticket: string | null; start: number; end: number; outcome: string }
export interface Lane { thread: string; spans: Span[] }
export interface StageStat { count: number; p50: number; p95: number; max: number }
export interface ModelStat { calls: number; cached: number; tokens: number; p50: number; p95: number }
export interface Fault { class: string; label: string; severity: Severity; layer: string; handling: string; count: number; last: number | null }
export interface Recent {
  ticket: string; outcome: string; ms: number | null; t: number;
  action?: string; priority?: string | null; language?: string | null; row?: number | null; confidence?: number | null;
  error?: string | null; resumed?: boolean;
}
export interface RunInfo { id: string; status: string; started: number; elapsed: number; workers: number | null; dry_run: boolean | null; total: number }

export interface Snapshot {
  now: number;
  run: RunInfo | null;
  progress?: { done: number; total: number; outcomes: Record<string, number>; actions: Record<string, number>; tickets_per_min: number };
  stages?: Record<string, StageStat>;
  lanes?: Lane[];
  http?: { calls: number; by_status: Record<string, number>; p50: number; p95: number; per_second: { t: number; n: number }[]; limit_rps: number };
  models?: Record<'judge' | 'draft' | 'note', ModelStat>;
  tokens_per_minute?: { series: { minute: number; tokens: number }[]; limit: number };
  faults?: Fault[];
  activity?: Record<string, number>;
  active_threads?: number;
  ticket_ms?: { p50: number; p95: number };
  recent?: Recent[];
}

export interface JobState {
  status: 'idle' | 'running';
  kind?: string; run_id?: string; dry_run?: boolean; workers?: number; started?: number;
  last?: { kind: string; run_id: string; exit_code: number; message: string; summary: Record<string, any>; finished: number } | null;
}

export interface RunListItem { run_id: string; started: number; last: number; tickets: number; faults: number }
