import { useEffect, useMemo, useRef, useState } from 'react';
import { Panel } from './Panel';

/** "Try a ticket": decide one ticket with either decision model (or both). Read-only: nothing is posted anywhere. */

interface CaseRow { id: string; subject: string; body: string; channel?: string; customer_plan?: string; label?: Record<string, unknown> }
interface Group { id: string; title: string; cases: CaseRow[] }
interface Catalog { groups: Group[]; notes: string[] }

type Judge = 'gemini' | 'typesafe';
type Pick = Judge | 'both';
type Source = 'case' | 'custom';
type Entry = 'fields' | 'json';

interface TryResult {
  model: { judge: string; provider: string; draft: string; retrieval: string; second_opinion: boolean };
  decision: { action: string; category: string; priority: string; language: string; reason: string; confidence: number; row: number | string; faq_ids: string[]; triggers: Record<string, number> };
  reply: string; handoff_note: string; english_gloss: string;
  verification: { ran: boolean; failures: string[] };
  judgments: { yes_no: Record<string, number>; choices: Record<string, Record<string, number>>; faq: [string, number][] };
  borderline: string[]; resampled: boolean; second_opinion: { verdict?: string; [k: string]: unknown } | null; notes: string[];
  cost: { judge_calls: number; judge_input_tokens: number; verify_input_tokens: number; draft_in: number; draft_out: number; note_in: number; note_out: number };
  seconds: { decide: number; total: number };
  posted: boolean;
}
type Outcome = { judge: Judge; result?: TryResult; error?: string };

const EXAMPLE = JSON.stringify({ subject: 'Rooster niet zichtbaar', body: 'Hoi, mijn team kan het nieuwe rooster niet zien in de app. Kunnen jullie helpen?', customer_plan: 'Plus', channel: 'email' }, null, 2);
const JUDGE_NAME: Record<Judge, string> = { gemini: 'Gemini Decision 1', typesafe: 'TypeSafe Jev 1.14' };

async function post(body: unknown, signal: AbortSignal): Promise<TryResult> {
  const r = await fetch('/api/try', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-QRA': '1' }, body: JSON.stringify(body), signal });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error ?? `The server answered ${r.status}.`);
  return d as TryResult;
}

function jsonProblem(text: string): string {
  if (!text.trim()) return 'Paste a JSON object with a subject and/or a body.';
  try {
    const v = JSON.parse(text);
    if (v === null || typeof v !== 'object' || Array.isArray(v)) return 'A ticket is a JSON object, like {"subject": "...", "body": "..."}.';
    const t = (v as { ticket?: unknown }).ticket && typeof (v as { ticket?: unknown }).ticket === 'object' ? (v as { ticket: Record<string, unknown> }).ticket : (v as Record<string, unknown>);
    if (!t.subject && !t.body && !t.message && !t.text && !t.title) return 'Give the ticket a subject or a body.';
    return '';
  } catch (e) { return `Not valid JSON yet: ${(e as Error).message}`; }
}

const pct = (v: number) => `${Math.round(v * 100)}%`;
const n = (v: number) => v.toLocaleString('en-US');

export function TryTicket() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [source, setSource] = useState<Source>('case');
  const [entry, setEntry] = useState<Entry>('fields');
  const [groupId, setGroupId] = useState('');
  const [caseId, setCaseId] = useState('');
  const [fields, setFields] = useState({ subject: '', body: '', customer_plan: '', channel: 'email' });
  const [jsonText, setJsonText] = useState(EXAMPLE);
  const [pick, setPick] = useState<Pick>('gemini');
  const [opinion, setOpinion] = useState(true);
  const [retrieval, setRetrieval] = useState<'exhaustive' | 'cascade'>('exhaustive');
  const [busy, setBusy] = useState(false);
  const [outcomes, setOutcomes] = useState<Outcome[]>([]);
  const [shownCase, setShownCase] = useState<CaseRow | null>(null);
  const [msg, setMsg] = useState('');
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    fetch('/api/cases').then((r) => r.json()).then((c: Catalog) => {
      if (!Array.isArray(c?.groups)) throw new Error('unexpected answer');  // an error object must never be treated as the list
      setCatalog(c);
      const g = c.groups[0];
      if (g) { setGroupId(g.id); setCaseId(g.cases[0]?.id ?? ''); }
    }).catch(() => setCatalog({ groups: [], notes: ['The case list could not be loaded (the server may have restarted: reload the page). Custom input still works.'] }));
    return () => abort.current?.abort();
  }, []);

  const group = useMemo(() => catalog?.groups.find((g) => g.id === groupId), [catalog, groupId]);
  const chosen = useMemo(() => group?.cases.find((c) => c.id === caseId), [group, caseId]);
  const problem = source === 'custom' && entry === 'json' ? jsonProblem(jsonText) : '';
  const fieldsEmpty = source === 'custom' && entry === 'fields' && !fields.subject.trim() && !fields.body.trim();
  const canRun = !busy && (source === 'case' ? !!chosen : entry === 'json' ? !problem : !fieldsEmpty);

  const ticketPayload = (): Record<string, unknown> => {
    if (source === 'case' && chosen) return { ticket: { subject: chosen.subject, body: chosen.body, customer_plan: chosen.customer_plan ?? '', channel: chosen.channel ?? '' } };
    if (entry === 'json') return { json: jsonText };
    return { ticket: { ...fields } };
  };

  const switchEntry = (next: Entry) => {
    if (next === entry) return;
    if (next === 'json') {  // carry the typed fields over
      setJsonText(JSON.stringify(Object.fromEntries(Object.entries(fields).filter(([, v]) => v)), null, 2));
    } else if (!jsonProblem(jsonText)) {  // and the pasted JSON back
      const raw = JSON.parse(jsonText) as Record<string, unknown>;
      const t = (raw.ticket && typeof raw.ticket === 'object' ? raw.ticket : raw) as Record<string, unknown>;
      const s = (k: string, ...alt: string[]) => String([k, ...alt].map((a) => t[a]).find((v) => typeof v === 'string') ?? '');
      setFields({ subject: s('subject', 'title'), body: s('body', 'message', 'text'), customer_plan: s('customer_plan', 'plan'), channel: s('channel') || 'email' });
    }
    setEntry(next);
  };

  const editAsCustom = () => {
    if (!chosen) return;
    setFields({ subject: chosen.subject, body: chosen.body, customer_plan: chosen.customer_plan ?? '', channel: chosen.channel || 'email' });
    setEntry('fields'); setSource('custom');
  };

  const run = async () => {
    abort.current?.abort();
    const ctl = new AbortController();
    abort.current = ctl;
    const judges: Judge[] = pick === 'both' ? ['gemini', 'typesafe'] : [pick];
    setBusy(true); setMsg(judges.length > 1 ? 'Deciding with both models…' : `Deciding with ${JUDGE_NAME[judges[0]]}…`); setOutcomes([]);
    setShownCase(source === 'case' ? chosen ?? null : null);
    const timer = setTimeout(() => ctl.abort(), 150_000);
    try {
      const results = await Promise.all(judges.map(async (judge): Promise<Outcome> => {
        try { return { judge, result: await post({ ...ticketPayload(), judge, retrieval, second_opinion: opinion }, ctl.signal) }; }
        catch (e) { return { judge, error: (e as Error).name === 'AbortError' ? 'It took too long or was cancelled. Nothing was posted.' : (e as Error).message }; }
      }));
      setOutcomes(results);
      setMsg(results.every((r) => r.error) ? 'Nothing could be decided.' : 'Done. Nothing was posted.');
    } finally { clearTimeout(timer); setBusy(false); }
  };

  return (
    <Panel i={1}>
      <Panel.Header title="Try a Ticket"><Panel.Hint>Decide one ticket with either decision model. Read-only: nothing is posted to the helpdesk.</Panel.Hint></Panel.Header>
      <div className="try">
        <div className="try-in">
          <div className="seg" role="group" aria-label="Where the ticket comes from">
            <button type="button" className="btn" aria-pressed={source === 'case'} onClick={() => setSource('case')}>Test case</button>
            <button type="button" className="btn" aria-pressed={source === 'custom'} onClick={() => setSource('custom')}>Custom ticket</button>
          </div>

          {source === 'case' && (
            <div className="try-case">
              {catalog?.notes.map((t) => <p key={t} className="muted try-note">{t}</p>)}
              <label className="tfield">Set
                <select value={groupId} onChange={(e) => { setGroupId(e.target.value); setCaseId(catalog?.groups.find((g) => g.id === e.target.value)?.cases[0]?.id ?? ''); }}>
                  {catalog?.groups.map((g) => <option key={g.id} value={g.id}>{g.title} ({g.cases.length})</option>)}
                </select>
              </label>
              <label className="tfield">Ticket
                <select value={caseId} onChange={(e) => setCaseId(e.target.value)} disabled={!group?.cases.length}>
                  {group?.cases.map((c) => <option key={c.id} value={c.id}>{c.id}: {c.subject.slice(0, 60) || '(no subject)'}</option>)}
                </select>
              </label>
              {chosen ? (
                <div className="preview">
                  <b>{chosen.subject || '(no subject)'}</b>
                  <p>{chosen.body || '(no body)'}</p>
                  <small className="muted">{[chosen.customer_plan && `plan ${chosen.customer_plan}`, chosen.channel].filter(Boolean).join(' · ') || 'no plan or channel'}</small>
                  {chosen.label && <small className="muted">Expected: {Object.entries(chosen.label).filter(([k]) => k !== 'reason').map(([k, v]) => `${k} ${String(v)}`).join(', ')}</small>}
                  <button type="button" className="btn" onClick={editAsCustom}>Edit as a custom ticket</button>
                </div>
              ) : <p className="muted">No cases to show.</p>}
            </div>
          )}

          {source === 'custom' && (
            <div className="try-custom">
              <div className="seg" role="group" aria-label="How to enter the ticket">
                <button type="button" className="btn" aria-pressed={entry === 'fields'} onClick={() => switchEntry('fields')}>Fill in fields</button>
                <button type="button" className="btn" aria-pressed={entry === 'json'} onClick={() => switchEntry('json')}>Paste JSON</button>
              </div>
              {entry === 'fields' ? (
                <>
                  <label className="tfield">Subject
                    <input type="text" name="subject" autoComplete="off" maxLength={300} value={fields.subject} onChange={(e) => setFields({ ...fields, subject: e.target.value })} placeholder="e.g. Factuur downloaden" />
                  </label>
                  <label className="tfield">Message
                    <textarea name="body" rows={6} maxLength={8000} value={fields.body} onChange={(e) => setFields({ ...fields, body: e.target.value })} placeholder="What the customer wrote, in any language" />
                  </label>
                  <div className="two">
                    <label className="tfield">Customer plan (optional)
                      <input type="text" name="customer_plan" autoComplete="off" maxLength={40} value={fields.customer_plan} onChange={(e) => setFields({ ...fields, customer_plan: e.target.value })} placeholder="e.g. Plus" />
                    </label>
                    <label className="tfield">Channel (optional)
                      <input type="text" name="channel" autoComplete="off" maxLength={40} list="channels" value={fields.channel} onChange={(e) => setFields({ ...fields, channel: e.target.value })} />
                      <datalist id="channels"><option value="email" /><option value="chat" /><option value="web" /></datalist>
                    </label>
                  </div>
                </>
              ) : (
                <label className="tfield">Ticket as JSON
                  <textarea className="mono" name="ticket-json" rows={10} spellCheck={false} value={jsonText} onChange={(e) => setJsonText(e.target.value)} aria-invalid={!!problem} aria-describedby="json-problem" />
                  <small id="json-problem" className={problem ? 'bad' : 'muted'} role="status" aria-live="polite">{problem || 'Valid. Fields: subject, body (or message), customer_plan (or plan), channel. Other fields are ignored.'}</small>
                </label>
              )}
            </div>
          )}

          <fieldset className="methodbox">
            <legend>Decision model</legend>
            {([['gemini', 'Gemini Decision 1', 'default'], ['typesafe', 'TypeSafe Jev 1.14', 'needs TYPESAFE_API_KEY'], ['both', 'Compare both', 'runs both side by side']] as const).map(([id, title, text]) => (
              <label key={id} className="opt">
                <input type="radio" name="try-judge" checked={pick === id} onChange={() => setPick(id)} />
                <b>{title}</b><small>{text}</small>
              </label>
            ))}
          </fieldset>
          <div className="two">
            <label className="tcheck"><input type="checkbox" checked={opinion} onChange={(e) => setOpinion(e.target.checked)} /> Second-opinion agent for borderline replies</label>
            <label className="tfield">Retrieval
              <select value={retrieval} onChange={(e) => setRetrieval(e.target.value as 'exhaustive' | 'cascade')}>
                <option value="exhaustive">Score every entry (original)</option>
                <option value="cascade">Cascade shortlist</option>
              </select>
            </label>
          </div>
          <div className="row">
            <span className="msg" role="status" aria-live="polite">{msg}</span>
            <button type="button" className="btn primary" disabled={!canRun} onClick={run}>{busy ? 'Deciding…' : 'Decide this ticket'}</button>
          </div>
        </div>

        <div className="try-out" aria-live="polite">
          {!outcomes.length && !busy && <p className="empty"><b>No result yet.</b><br />Pick a test case or enter a ticket, choose a model, and press Decide.</p>}
          {busy && <p className="empty"><b>Working…</b><br />A decision takes about 3 to 15 seconds, longer if the model is rate-limited.</p>}
          {outcomes.length === 2 && outcomes.every((o) => o.result) && (
            <p className="agree">{outcomes[0].result!.decision.action === outcomes[1].result!.decision.action
              ? `Both models chose to ${outcomes[0].result!.decision.action}.` : `The models disagree: ${outcomes.map((o) => `${JUDGE_NAME[o.judge]} chose ${o.result!.decision.action}`).join(', ')}.`}</p>
          )}
          <div className={outcomes.length > 1 ? 'cards pair' : 'cards'}>
            {outcomes.map((o) => <ResultCard key={o.judge} outcome={o} expected={shownCase?.label} />)}
          </div>
        </div>
      </div>
    </Panel>
  );
}

function ResultCard({ outcome, expected }: { outcome: Outcome; expected?: Record<string, unknown> }) {
  const { result: r, judge, error } = outcome;
  if (!r) return <article className="rcard"><h3>{JUDGE_NAME[judge]}</h3><p className="bad" role="alert">{error}</p></article>;
  const d = r.decision;
  const got: Record<string, string> = { action: d.action, priority: d.priority, category: d.category, language: d.language, faq: d.faq_ids[0] ?? '' };
  const checks = expected ? Object.entries(expected).filter(([k]) => k in got && expected[k] != null && k !== 'reason') : [];
  const notable = Object.entries(r.judgments.yes_no).filter(([, v]) => v >= 0.2).sort((a, b) => b[1] - a[1]);
  const c = r.cost;
  return (
    <article className="rcard">
      <h3>{JUDGE_NAME[judge]} <small className="muted">{r.model.judge}</small></h3>
      <p className="verdict"><span className={`tag ${d.action}`}>{d.action}</span> <span className="tag">{d.priority}</span> <span className="tag">{d.category}</span> <span className="tag">{d.language}</span></p>
      <p className="reason">{d.reason}</p>
      <p className="muted small">Rule {String(d.row)} · confidence {pct(d.confidence)}{r.resampled ? ' · asked twice (borderline)' : ''}{r.second_opinion ? ` · second opinion: ${String(r.second_opinion.verdict ?? 'asked')}` : ''}</p>

      {checks.length > 0 && (
        <table className="checks"><thead><tr><th>Expected</th><th>Got</th><th /></tr></thead><tbody>
          {checks.map(([k, v]) => { const ok = String(v) === got[k]; return <tr key={k}><td>{k} {String(v)}</td><td>{got[k] || 'none'}</td><td className={ok ? 'ok' : 'bad'}>{ok ? '✓' : '✗'}</td></tr>; })}
        </tbody></table>
      )}

      {r.reply && <><h4>Reply that would be sent</h4><pre className="text">{r.reply}</pre></>}
      {r.handoff_note && <><h4>Handoff note for the support team</h4><pre className="text">{r.handoff_note}</pre></>}
      {r.english_gloss && <p className="muted small">In English: {r.english_gloss}</p>}
      {r.verification.failures.length > 0 && (
        <div className="bad" role="alert"><b>A drafted reply was rejected, so this became a handoff:</b><ul>{r.verification.failures.map((f) => <li key={f}>{f}</li>)}</ul></div>
      )}

      <details className="tableview"><summary>What the model judged</summary>
        <table><thead><tr><th>Question</th><th className="r">Probability</th></tr></thead><tbody>
          {notable.map(([k, v]) => <tr key={k}><td className="mono">{k}</td><td className="r num">{pct(v)}</td></tr>)}
          {r.judgments.faq.map(([k, v]) => <tr key={k}><td className="mono">help-centre {k}</td><td className="r num">{pct(v)}</td></tr>)}
          {Object.entries(r.judgments.choices).map(([k, v]) => <tr key={k}><td className="mono">{k}</td><td className="r num">{Object.entries(v).map(([o, p]) => `${o} ${pct(p)}`).join(', ')}</td></tr>)}
        </tbody></table>
      </details>
      <p className="muted small">{r.seconds.total}s in total ({r.seconds.decide}s to decide) · {c.judge_calls} decision calls, {n(c.judge_input_tokens + c.verify_input_tokens)} decision tokens · draft {n(c.draft_in + c.note_in)} in / {n(c.draft_out + c.note_out)} out · nothing was posted</p>
    </article>
  );
}
