import { useCallback, useEffect, useState } from "react";

import { type ApiProblem, toProblem } from "./api/client";

export interface Polled<T> {
  data: T | undefined;
  error: ApiProblem | undefined;
  loading: boolean;
  reload: () => void;
}

const RETRY_MS = 5000;

/** Load once, then again after `delay(data)` ms (null stops). Failures keep the last data. */
export function usePoll<T>(load: () => Promise<T>, delay: (data: T) => number | null): Polled<T> {
  const [state, setState] = useState<{ data?: T; error?: ApiProblem; loading: boolean }>({
    loading: true,
  });
  const [round, setRound] = useState(0);
  const reload = useCallback(() => setRound((n) => n + 1), []);

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const tick = async () => {
      let next: number | null = RETRY_MS;
      try {
        const data = await load();
        if (!alive) return;
        setState({ data, loading: false });
        next = delay(data);
      } catch (error) {
        if (!alive) return;
        setState((prev) => ({ data: prev.data, error: toProblem(error), loading: false }));
      }
      if (next !== null) timer = window.setTimeout(() => void tick(), next);
    };
    void tick();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [load, delay, round]);

  return { data: state.data, error: state.error, loading: state.loading, reload };
}
