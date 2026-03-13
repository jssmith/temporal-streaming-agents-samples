"use client";

import ThinkingStep from "./ThinkingStep";
import CodeExecution from "./CodeExecution";
import StreamingMarkdown from "./StreamingMarkdown";

export interface ThinkingStepData {
  id: string;
  status: "active" | "done";
  content: string;
}

export interface ToolCallData {
  callId: string;
  toolName: string;
  arguments: Record<string, unknown>;
  status: "running" | "done" | "error";
  result?: Record<string, unknown>;
  error?: string;
}

export type Step =
  | { type: "thinking"; data: ThinkingStepData }
  | { type: "tool_call"; data: ToolCallData }
  | { type: "output"; text: string };

interface UserMessageProps {
  content: string;
  onEdit?: (newContent: string) => void;
}

export function UserMessage({ content }: UserMessageProps) {
  return (
    <div className="flex justify-end mb-4">
      <div className="bg-accent rounded-2xl rounded-br-md px-4 py-2.5 max-w-[75%] text-sm">
        {content}
      </div>
    </div>
  );
}

interface AgentMessageProps {
  steps: Step[];
}

export function AgentMessage({ steps }: AgentMessageProps) {
  return (
    <div className="mb-4">
      <div className="space-y-1">
        {steps.map((step, i) => {
          if (step.type === "thinking") {
            return (
              <ThinkingStep
                key={`thinking-${step.data.id}`}
                status={step.data.status}
                content={step.data.content}
                isLast={i === steps.length - 1}
              />
            );
          }
          if (step.type === "tool_call") {
            return (
              <CodeExecution
                key={`tool-${step.data.callId}`}
                callId={step.data.callId}
                toolName={step.data.toolName}
                arguments={step.data.arguments}
                status={step.data.status}
                result={step.data.result}
                error={step.data.error}
              />
            );
          }
          if (step.type === "output") {
            return (
              <div key={`output-${i}`} className="mt-2">
                <StreamingMarkdown content={step.text} />
              </div>
            );
          }
          return null;
        })}
      </div>
    </div>
  );
}
