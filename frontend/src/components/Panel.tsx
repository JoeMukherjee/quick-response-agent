import type { CSSProperties, ReactNode } from 'react';

/** Compound component: consumers compose the pieces they need, no prop drilling or boolean modes. */
function Root({ children, className = '', i = 0 }: { children: ReactNode; className?: string; i?: number }) {
  return <section className={`panel reveal-up ${className}`.trim()} style={{ '--i': i } as CSSProperties}>{children}</section>;
}

function Header({ title, children }: { title: string; children?: ReactNode }) {
  return <header><h2>{title}</h2>{children}</header>;
}

function Hint({ children }: { children: ReactNode }) {
  return <span className="hint">{children}</span>;
}

export const Panel = Object.assign(Root, { Header, Hint });
