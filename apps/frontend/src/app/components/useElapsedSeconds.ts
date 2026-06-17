import { useState, useEffect, useRef } from "react";

// Wall-clock elapsed-time ticker shared by ThinkingStep and CodeExecution.
//
// While `isActive` is true, it ticks `elapsed` (seconds since mount) every
// 100ms; on the active -> inactive transition it captures one final reading
// so a finished step shows its true end time rather than the last tick.
//
// `frozenDuration` is a pre-computed duration derived from backend event
// timestamps (used on replay, where wall-clock since mount is meaningless).
// When provided it wins; otherwise the live elapsed value is returned.
export function useElapsedSeconds(
  isActive: boolean,
  frozenDuration?: number,
): number {
  const [elapsed, setElapsed] = useState(0);
  const startTime = useRef(Date.now());

  useEffect(() => {
    if (!isActive) return;
    const interval = setInterval(() => {
      setElapsed((Date.now() - startTime.current) / 1000);
    }, 100);
    return () => clearInterval(interval);
  }, [isActive]);

  useEffect(() => {
    if (!isActive) {
      setElapsed((Date.now() - startTime.current) / 1000);
    }
  }, [isActive]);

  return frozenDuration ?? elapsed;
}
