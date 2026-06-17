"use client";

import { useState, useEffect } from "react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { codeHighlighterStyle, codeHighlighterCustomStyle } from "./codeStyle";
import { useElapsedSeconds } from "./useElapsedSeconds";

interface CodeExecutionProps {
  toolName: string;
  arguments: Record<string, unknown>;
  status: "running" | "done" | "error";
  result?: Record<string, unknown>;
  error?: string;
  duration?: number; // Pre-computed from event timestamps (for replay)
}

// Per-tool presentation: syntax-highlight language, human label, and which
// argument holds the code to show. Tools not listed fall back to DEFAULT.
type ToolMeta = {
  language: string;
  label: string;
  codeArg: string;
};

const DEFAULT_TOOL_META: ToolMeta = { language: "bash", label: "bash", codeArg: "command" };

const TOOL_META: Record<string, ToolMeta> = {
  execute_sql: { language: "sql", label: "SQL", codeArg: "query" },
  execute_python: { language: "python", label: "Python", codeArg: "code" },
};

function toolMeta(toolName: string): ToolMeta {
  return TOOL_META[toolName] ?? DEFAULT_TOOL_META;
}

function formatResult(result: Record<string, unknown>): string {
  if (result.rows) {
    const rows = result.rows as Record<string, unknown>[];
    if (rows.length === 0) return "(no rows)";
    return JSON.stringify(rows, null, 2);
  }
  if (result.output) return result.output as string;
  return JSON.stringify(result, null, 2);
}

export default function CodeExecution({
  toolName,
  arguments: args,
  status,
  result,
  error,
  duration,
}: CodeExecutionProps) {
  const [expanded, setExpanded] = useState(status === "running");

  // Collapse once the tool finishes (the live, expanded view is only useful
  // while running).
  useEffect(() => {
    if (status !== "running") {
      setExpanded(false);
    }
  }, [status]);

  const { language, label, codeArg } = toolMeta(toolName);
  const code = (args[codeArg] as string) || "";

  const displayDuration = useElapsedSeconds(status === "running", duration);

  const statusLabel =
    status === "running"
      ? `Running ${label}... ${displayDuration.toFixed(1)}s`
      : status === "error"
        ? `${label} failed · ${displayDuration.toFixed(1)}s`
        : `Executed ${label} · ${displayDuration.toFixed(1)}s`;

  return (
    <div className="mb-1">
      <button
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
        className="flex items-center gap-1.5 text-[13px] font-medium text-gray-400 hover:text-gray-300 transition-colors"
      >
        <span className="text-xs">{expanded ? "▾" : "▸"}</span>
        <span
          className={
            status === "running"
              ? "animate-pulse-subtle"
              : status === "error"
                ? "text-red-400"
                : "text-green-400/80"
          }
        >
          {statusLabel}
        </span>
      </button>
      {expanded && (
        <div className="ml-4 mt-1 pl-3 border-l-2 border-accent/40 space-y-2">
          <div className="relative rounded-md overflow-hidden">
            <div className="absolute top-1 right-2 text-[11px] text-gray-500 uppercase">
              {language}
            </div>
            <SyntaxHighlighter
              language={language}
              style={codeHighlighterStyle}
              customStyle={codeHighlighterCustomStyle}
            >
              {code}
            </SyntaxHighlighter>
          </div>
          {result && (
            <pre className="text-[12px] text-gray-400 bg-[#1e1e3a] rounded-md p-3 overflow-x-auto max-h-60 overflow-y-auto whitespace-pre-wrap">
              {formatResult(result)}
            </pre>
          )}
          {error && (
            <pre className="text-[12px] text-red-400 bg-red-950/30 rounded-md p-3 overflow-x-auto whitespace-pre-wrap">
              {error}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
