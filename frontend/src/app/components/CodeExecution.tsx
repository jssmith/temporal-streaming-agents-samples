"use client";

import { useState, useEffect, useRef } from "react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { vscDarkPlus } from "react-syntax-highlighter/dist/esm/styles/prism";

interface CodeExecutionProps {
  callId: string;
  toolName: string;
  arguments: Record<string, unknown>;
  status: "running" | "done" | "error";
  result?: Record<string, unknown>;
  error?: string;
}

function getLanguage(toolName: string): string {
  if (toolName === "execute_sql") return "sql";
  if (toolName === "execute_python") return "python";
  return "bash";
}

function getLabel(toolName: string): string {
  if (toolName === "execute_sql") return "SQL";
  if (toolName === "execute_python") return "Python";
  return "bash";
}

function getCode(toolName: string, args: Record<string, unknown>): string {
  if (toolName === "execute_sql") return (args.query as string) || "";
  if (toolName === "execute_python") return (args.code as string) || "";
  return (args.command as string) || "";
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
  callId,
  toolName,
  arguments: args,
  status,
  result,
  error,
}: CodeExecutionProps) {
  const [expanded, setExpanded] = useState(status === "running");
  const [elapsed, setElapsed] = useState(0);
  const startTime = useRef(Date.now());

  useEffect(() => {
    if (status !== "running") return;
    const interval = setInterval(() => {
      setElapsed((Date.now() - startTime.current) / 1000);
    }, 100);
    return () => clearInterval(interval);
  }, [status]);

  useEffect(() => {
    if (status !== "running") {
      setElapsed((Date.now() - startTime.current) / 1000);
      setExpanded(false);
    }
  }, [status]);

  const label = getLabel(toolName);
  const language = getLanguage(toolName);
  const code = getCode(toolName, args);

  const statusLabel =
    status === "running"
      ? `Running ${label}... ${elapsed.toFixed(1)}s`
      : status === "error"
        ? `${label} failed · ${elapsed.toFixed(1)}s`
        : `Executed ${label} · ${elapsed.toFixed(1)}s`;

  return (
    <div className="mb-1">
      <button
        onClick={() => setExpanded(!expanded)}
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
              style={vscDarkPlus}
              customStyle={{
                margin: 0,
                padding: "0.75rem",
                fontSize: "0.8125rem",
                background: "#1e1e3a",
                borderRadius: "0.375rem",
              }}
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
