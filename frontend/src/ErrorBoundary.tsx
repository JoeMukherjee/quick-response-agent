import { Component, type ReactNode } from 'react';

/** A crash in one part of the page must never leave a blank screen: say what happened and offer a reload. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(error: unknown) { console.error('dashboard error', error); }
  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <main style={{ padding: 32, maxWidth: 560 }}>
        <h1 style={{ fontSize: 18 }}>The dashboard hit an error</h1>
        <p>The server may have restarted or been rebuilt. Your runs and answers are not affected.</p>
        <button className="btn primary" type="button" onClick={() => location.reload()}>Reload the page</button>
      </main>
    );
  }
}
