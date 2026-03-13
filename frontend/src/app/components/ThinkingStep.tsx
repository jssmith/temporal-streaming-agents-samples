"use client";

import { useState, useEffect, useRef } from "react";

interface ThinkingStepProps {
  status: "active" | "done";
  content: string;
  isLast?: boolean;
}

export default function ThinkingStep({ status, content, isLast }: ThinkingStepProps) {
  const [manualToggle, setManualToggle] = useState<boolean | null>(null);
  const expanded = manualToggle ?? (isLast === true);
  const [elapsed, setElapsed] = useState(0);

  // Reset manual override when this step is no longer the last item
  useEffect(() => {
    if (!isLast) {
      setManualToggle(null);
    }
  }, [isLast]);
  const startTime = useRef(Date.now());

  useEffect(() => {
    if (status !== "active") return;
    const interval = setInterval(() => {
      setElapsed((Date.now() - startTime.current) / 1000);
    }, 100);
    return () => clearInterval(interval);
  }, [status]);

  useEffect(() => {
    if (status === "done") {
      setElapsed((Date.now() - startTime.current) / 1000);
    }
  }, [status]);

  const label =
    status === "active"
      ? `Thinking... ${elapsed.toFixed(1)}s`
      : `Thought for ${elapsed.toFixed(1)}s`;

  return (
    <div className="mb-1">
      <button
        onClick={() => setManualToggle(!expanded)}
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
