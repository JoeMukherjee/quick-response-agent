import { createContext, use, useCallback, useMemo, useRef, useState, type MouseEvent, type ReactNode } from 'react';

interface Api { show: (content: ReactNode, e: MouseEvent | globalThis.MouseEvent) => void; hide: () => void }
const TooltipContext = createContext<Api>({ show: () => {}, hide: () => {} });
export const useTooltip = () => use(TooltipContext);

export function TooltipProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<{ content: ReactNode; x: number; y: number; on: boolean }>({ content: null, x: 0, y: 0, on: false });
  const box = useRef<HTMLDivElement>(null);
  const show = useCallback((content: ReactNode, e: { clientX: number; clientY: number }) => {
    const w = box.current?.offsetWidth ?? 200, h = box.current?.offsetHeight ?? 40;
    setState({ content, on: true, x: Math.min(e.clientX + 14, window.innerWidth - w - 8), y: Math.max(8, e.clientY - h - 12) });
  }, []);
  const hide = useCallback(() => setState((s) => ({ ...s, on: false })), []);
  const api = useMemo(() => ({ show, hide }), [show, hide]);
  return (
    <TooltipContext value={api}>
      {children}
      <div ref={box} className="tip" role="status" style={{ left: state.x, top: state.y, opacity: state.on ? 1 : 0 }}>{state.content}</div>
    </TooltipContext>
  );
}
