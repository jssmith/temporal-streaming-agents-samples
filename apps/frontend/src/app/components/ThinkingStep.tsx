"use client";

import { useState, useEffect } from "react";
import { useElapsedSeconds } from "./useElapsedSeconds";

interface ThinkingStepProps {
  status: "active" | "done";
  content: string;
  duration?: number; // Pre-computed from event timestamps (for replay)
  isLast?: boolean;
}

export default function ThinkingStep({ status, content, duration, isLast }: ThinkingStepProps) {
  const [manualToggle, setManualToggle] = useState<boolean | null>(null);
  const expanded = manualToggle ?? (isLast === true);

  // Reset manual override when this step is no longer the last item
  useEffect(() => {
    if (!isLast) {
      setManualToggle(null);
    }
  }, [isLast]);

  // Pre-computed duration from event timestamps wins (replay); otherwise
  // fall back to wall-clock elapsed time while active.
  const displayDuration = useElapsedSeconds(status === "active", duration);

  const label =
    status === "active"
      ? `Thinking... ${displayDuration.toFixed(1)}s`
      : `Thought for ${displayDuration.toFixed(1)}s`;

  return (
    <div className="mb-1">
      <button
        onClick={() => setManualToggle(!expanded)}
        aria-expanded={expanded}
        className="flex items-center gap-1.5 text-[13px] font-medium text-gray-400 hover:text-gray-300 transition-colors"
      >
        <span className="text-xs">{expanded ? "▾" : "▸"}</span>
        <span className={status === "active" ? "animate-pulse-subtle" : ""}>
          {label}
        </span>
      </button>
      {expanded && content && (
        <div className="ml-4 mt-1 pl-3 border-l-2 border-accent/40 text-[13px] text-gray-400 whitespace-pre-wrap">
          {content}
        </div>
      )}
    </div>
  );
}
