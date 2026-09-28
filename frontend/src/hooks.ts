import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "./api";

export interface Loaded<T> {
  data: T | undefined;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/**
 * Load data when the component mounts or `deps` change. With `pollMs`, load
 * again on that interval for as long as `shouldPoll(data)` holds.
 */
export function useLoad<T>(
  load: () => Promise<T>,
  deps: unknown[],
  options: { pollMs?: number; shouldPoll?: (data: T | undefined) => boolean } = {},
): Loaded<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;

  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loadRef
      .current()
      .then((value) => {
        if (cancelled) return;
        setData(value);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(describe(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const { pollMs, shouldPoll } = options;
  const polling = Boolean(pollMs && shouldPoll?.(data));
  useEffect(() => {
    if (!polling || !pollMs) return;
    const timer = window.setTimeout(reload, pollMs);
    return () => window.clearTimeout(timer);
  }, [polling, pollMs, reload, data]);

  return { data, error, loading, reload };
}

export function describe(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return "Something went wrong.";
}
