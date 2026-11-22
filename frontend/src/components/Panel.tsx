import type { ReactNode } from 'react';

/** Compound component: consumers compose the pieces they need, no prop drilling or boolean modes. */
function Root({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <section className={`panel ${className}`.trim()}>{children}</section>;
}

function Header({ title, children }: { title: string; children?: ReactNode }) {
  return <header><h2>{title}</h2>{children}</header>;
}

function Hint({ children }: { children: ReactNode }) {
  return <span className="hint">{children}</span>;
}

export const Panel = Object.assign(Root, { Header, Hint });
