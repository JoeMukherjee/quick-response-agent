import { useState } from 'react';
import { Bench } from './components/Bench';
import { Decisions, RequestsPerSecond, StageLatency, TokensPerMinute } from './components/Charts';
import { Flow } from './components/Flow';
import { Header } from './components/Header';
import { Kpis } from './components/Kpis';
import { DecisionTable, LaneTable, RpsTable, StageTable, TokenTable } from './components/TableViews';
import { Lanes, Legend } from './components/Lanes';
import { Panel } from './components/Panel';
import { FaultTable, RecentTickets } from './components/Tables';
import { TooltipProvider } from './components/Tooltip';
import { TryTicket } from './components/TryTicket';
import { useTelemetry } from './hooks';

export default function App() {
  const { snap, job, runs, runId, setRunId, lost, loadRuns } = useTelemetry();
  const [bench, setBench] = useState(false);
  return (
    <TooltipProvider>
      <a className="skip" href="#main">Skip to main content</a>
      <Header snap={snap} job={job} runs={runs} runId={runId} lost={lost} onRun={setRunId} onBench={() => setBench(true)} />
      <main id="main">
        <Panel i={0}>
          <Panel.Header title="End to End"><Panel.Hint>Every ticket takes this path. Numbers are live from telemetry.</Panel.Hint></Panel.Header>
          <div className="flow-wrap"><Flow snap={snap} /></div>
        </Panel>

        <TryTicket />

        <Kpis snap={snap} />

        <section className="grid-2">
          <Panel i={3}><Panel.Header title="Parallel Workers"><Legend /></Panel.Header><Lanes snap={snap} /><LaneTable snap={snap} /></Panel>
          <Panel i={4}><Panel.Header title="Stage Latency"><Panel.Hint>median, with 95th percentile</Panel.Hint></Panel.Header><StageLatency snap={snap} /><StageTable snap={snap} /></Panel>
        </section>

        <section className="grid-3">
          <Panel i={5}><Panel.Header title="Helpdesk Requests per Second" /><RequestsPerSecond snap={snap} /><RpsTable snap={snap} /></Panel>
          <Panel i={6}><Panel.Header title="Decision Tokens per Minute" /><TokensPerMinute snap={snap} /><TokenTable snap={snap} /></Panel>
          <Panel i={7}><Panel.Header title="What the Agent Decided" /><Decisions snap={snap} /><DecisionTable snap={snap} /></Panel>
        </section>

        <Panel className="defer" i={8}>
          <Panel.Header title="Failure Taxonomy"><Panel.Hint>Every failure class is listed, including the ones that did not happen.</Panel.Hint></Panel.Header>
          <FaultTable snap={snap} />
        </Panel>

        <Panel className="defer" i={9}>
          <Panel.Header title="Recent Tickets"><Panel.Hint>Ids and decisions only. Ticket text is never stored.</Panel.Hint></Panel.Header>
          <RecentTickets snap={snap} />
        </Panel>
      </main>
      <Bench open={bench} onClose={() => setBench(false)} job={job} onStarted={() => { setRunId(null); setTimeout(loadRuns, 800); }} />
    </TooltipProvider>
  );
}
