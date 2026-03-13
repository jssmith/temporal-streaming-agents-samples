"use client";

import { useState, useRef, useEffect, useCallback, useReducer } from "react";
import { UserMessage, AgentMessage, Step, ToolCallData } from "./components/ChatMessage";
import Sidebar, { SessionTab } from "./components/Sidebar";

const SUGGESTED_PROMPTS = [
  "Build a bar chart of the top 10 genres by revenue",
  "Which genres are most popular by country? Show a pivot table",
  "Compare monthly revenue trends across years using pandas",
  "Run a customer segmentation analysis with spending tiers",
  "Find the top 5 artists by track count and revenue side by side",
];

// --- Types ---

interface SSEEvent {
  type: string;
  timestamp: string;
  data: Record<string, unknown>;
}

interface ChatMessage {
  role: "user" | "agent";
  content?: string;
  steps?: Step[];
}

type AppState = "idle" | "sending" | "running" | "error";

// --- Chat state reducer ---
// All UI state is derived from events. The reducer handles:
// - USER_MESSAGE: adds a user message
// - AGENT_COMPLETE: snapshots the in-progress turn into a completed agent message
// - Turn events (thinking, tool calls, text): update the in-progress turn

interface TurnState {
  steps: Step[];
  thinkingCounter: number;
}

interface ChatState {
  messages: ChatMessage[];
  currentTurn: TurnState;
}

type ChatAction =
  | { type: "USER_MESSAGE"; content: string }
  | { type: "AGENT_COMPLETE" }
  | { type: "CLEAR" }
  | { type: "THINKING_START"; timestamp?: string }
  | { type: "THINKING_DELTA"; delta: string }
  | { type: "THINKING_COMPLETE"; content: string; timestamp?: string }
  | { type: "TOOL_CALL_START"; callId: string; toolName: string; arguments: Record<string, unknown>; timestamp?: string }
  | { type: "TOOL_CALL_COMPLETE"; callId: string; toolName: string; result?: Record<string, unknown>; error?: string; timestamp?: string }
  | { type: "TEXT_DELTA"; delta: string }
  | { type: "TEXT_COMPLETE"; text: string };

function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "USER_MESSAGE": {
      // Dedup: skip if last message is already this user message (optimistic add)
      const last = state.messages[state.messages.length - 1];
      if (last?.role === "user" && last.content === action.content) {
        return state;
      }
      return {
        ...state,
        messages: [...state.messages, { role: "user", content: action.content }],
      };
    }

    case "AGENT_COMPLETE": {
      if (state.currentTurn.steps.length === 0) return state;
      return {
        messages: [...state.messages, { role: "agent", steps: [...state.currentTurn.steps] }],
        currentTurn: { steps: [], thinkingCounter: 0 },
      };
    }

    case "CLEAR":
      return { messages: [], currentTurn: { steps: [], thinkingCounter: 0 } };

    // --- Turn events: update currentTurn ---
    default:
      return { ...state, currentTurn: turnReducer(state.currentTurn, action) };
  }
}

function turnReducer(state: TurnState, action: ChatAction): TurnState {
  const steps = [...state.steps];

  switch (action.type) {
    case "THINKING_START": {
      const hasActive = steps.some(
        (s) => s.type === "thinking" && s.data.status === "active"
      );
      if (hasActive) return state;
      const id = `t${state.thinkingCounter}`;
      steps.push({ type: "thinking", data: { id, status: "active", content: "", startedAt: action.timestamp } });
      return { ...state, steps, thinkingCounter: state.thinkingCounter + 1 };
    }
    case "THINKING_DELTA": {
      for (let i = steps.length - 1; i >= 0; i--) {
        const step = steps[i];
        if (step.type === "thinking" && step.data.status === "active") {
          steps[i] = {
            ...step,
            data: { ...step.data, content: step.data.content + action.delta },
          };
          break;
        }
      }
      return { ...state, steps };
    }
    case "THINKING_COMPLETE": {
      for (let i = steps.length - 1; i >= 0; i--) {
        const step = steps[i];
        if (step.type === "thinking" && step.data.status === "active") {
          let duration: number | undefined;
          if (step.data.startedAt && action.timestamp) {
            duration = (new Date(action.timestamp).getTime() - new Date(step.data.startedAt).getTime()) / 1000;
          }
          steps[i] = {
            ...step,
            data: { ...step.data, status: "done", content: action.content, duration },
          };
          break;
        }
      }
      return { ...state, steps };
    }
    case "TOOL_CALL_START": {
      // Remove empty placeholder thinking step if model went straight to tools
      const emptyIdx = steps.findIndex(
        (s) => s.type === "thinking" && s.data.status === "active" && !s.data.content
      );
      if (emptyIdx >= 0) steps.splice(emptyIdx, 1);
      steps.push({
        type: "tool_call",
        data: {
          callId: action.callId,
          toolName: action.toolName,
          arguments: action.arguments,
          status: "running",
          startedAt: action.timestamp,
        },
      });
      return { ...state, steps };
    }
    case "TOOL_CALL_COMPLETE": {
      const idx = steps.findIndex(
        (s) => s.type === "tool_call" && (s.data as ToolCallData).callId === action.callId
      );
      if (idx >= 0) {
        const step = steps[idx] as { type: "tool_call"; data: ToolCallData };
        let duration: number | undefined;
        if (step.data.startedAt && action.timestamp) {
          duration = (new Date(action.timestamp).getTime() - new Date(step.data.startedAt).getTime()) / 1000;
        }
        steps[idx] = {
          ...step,
          data: {
            ...step.data,
            status: action.error ? "error" : "done",
            result: action.result,
            error: action.error,
            duration,
          },
        };
      }
      return { ...state, steps };
    }
    case "TEXT_DELTA": {
      // Remove empty placeholder thinking step
      const emptyIdx = steps.findIndex(
        (s) => s.type === "thinking" && s.data.status === "active" && !s.data.content
      );
      if (emptyIdx >= 0) steps.splice(emptyIdx, 1);
      const lastStep = steps[steps.length - 1];
      if (lastStep?.type === "output") {
        steps[steps.length - 1] = {
          ...lastStep,
          text: lastStep.text + action.delta,
        };
      } else {
        steps.push({ type: "output", text: action.delta });
      }
      return { ...state, steps };
    }
    case "TEXT_COMPLETE": {
      const lastStep = steps[steps.length - 1];
      if (lastStep?.type === "output") {
        steps[steps.length - 1] = { ...lastStep, text: action.text };
      } else {
        steps.push({ type: "output", text: action.text });
      }
      return { ...state, steps };
    }
    default:
      return state;
  }
}

// --- Main Page ---

export default function Home() {
  const [sessions, setSessions] = useState<SessionTab[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [appState, setAppState] = useState<AppState>("idle");
  const [queuedMessage, setQueuedMessage] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const [chatState, dispatch] = useReducer(chatReducer, {
    messages: [],
    currentTurn: { steps: [], thinkingCounter: 0 },
  });

  // Auto-scroll
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [chatState.messages, chatState.currentTurn.steps]);

  // Fetch session list from backend on mount
  useEffect(() => {
    fetch("/api/sessions")
      .then((res) => res.json())
      .then((data: { session_id: string; message_count: number; preview: string }[]) => {
        const tabs: SessionTab[] = data.map((s) => ({
          sessionId: s.session_id,
          preview: s.preview,
          messageCount: s.message_count,
        }));
        setSessions(tabs);
        if (tabs.length > 0) {
          setActiveSessionId(tabs[0].sessionId);
          connectToStream(tabs[0].sessionId);
        }
      })
      .catch(() => {});
  }, []);

  // --- SSE processing ---

  function processEvent(event: SSEEvent) {
    const d = event.data;
    switch (event.type) {
      case "USER_MESSAGE":
        dispatch({ type: "USER_MESSAGE", content: d.content as string });
        break;
      case "AGENT_START":
        setAppState("running");
        break;
      case "AGENT_COMPLETE":
        dispatch({ type: "AGENT_COMPLETE" });
        setAppState("idle");
        break;
      case "THINKING_START":
        dispatch({ type: "THINKING_START", timestamp: event.timestamp });
        break;
      case "THINKING_DELTA":
        dispatch({ type: "THINKING_DELTA", delta: d.delta as string });
        break;
      case "THINKING_COMPLETE":
        dispatch({ type: "THINKING_COMPLETE", content: d.content as string, timestamp: event.timestamp });
        break;
      case "TOOL_CALL_START":
        dispatch({
          type: "TOOL_CALL_START",
          callId: d.call_id as string,
          toolName: d.tool_name as string,
          arguments: d.arguments as Record<string, unknown>,
          timestamp: event.timestamp,
        });
        break;
      case "TOOL_CALL_COMPLETE":
        dispatch({
          type: "TOOL_CALL_COMPLETE",
          callId: d.call_id as string,
          toolName: d.tool_name as string,
          result: d.result as Record<string, unknown> | undefined,
          error: d.error as string | undefined,
          timestamp: event.timestamp,
        });
        break;
      case "TEXT_DELTA":
        dispatch({ type: "TEXT_DELTA", delta: d.delta as string });
        break;
      case "TEXT_COMPLETE":
        dispatch({ type: "TEXT_COMPLETE", text: d.text as string });
        break;
    }
  }

  function consumeSSEStream(reader: ReadableStreamDefaultReader<Uint8Array>) {
    const decoder = new TextDecoder();
    let buffer = "";

    (async () => {
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n\n");
          buffer = lines.pop() || "";

          for (const chunk of lines) {
            if (!chunk.startsWith("data: ")) continue;
            try {
              const event: SSEEvent = JSON.parse(chunk.slice(6));
              processEvent(event);
            } catch {
              // skip malformed events
            }
          }
        }
        setAppState("idle");
      } catch (err: unknown) {
        if (err instanceof Error && err.name === "AbortError") {
          setAppState("idle");
        } else {
          setAppState("error");
        }
      }
    })();
  }

  // --- Session management ---

  function connectToStream(sessionId: string) {
    abortRef.current?.abort(); // Cancel any in-flight connection (strict mode double-mount)
    dispatch({ type: "CLEAR" });

    const controller = new AbortController();
    abortRef.current = controller;

    fetch(`/api/sessions/${sessionId}/stream?from_index=0`, {
      signal: controller.signal,
    })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        consumeSSEStream(res.body!.getReader());
      })
      .catch((err) => {
        if (!(err instanceof Error && err.name === "AbortError")) {
          setAppState("error");
        }
      });
  }

  function createNewSession() {
    if (appState === "running") {
      abortRef.current?.abort();
    }

    setActiveSessionId(null);
    dispatch({ type: "CLEAR" });
    setInput("");
    setAppState("idle");
    setQueuedMessage(null);

    setTimeout(() => inputRef.current?.focus(), 50);
  }

  function deleteSession(sessionId: string) {
    setSessions((prev) => {
      const updated = prev.filter((s) => s.sessionId !== sessionId);

      if (sessionId === activeSessionId) {
        if (updated.length > 0) {
          setActiveSessionId(updated[0].sessionId);
          connectToStream(updated[0].sessionId);
        } else {
          setActiveSessionId(null);
          dispatch({ type: "CLEAR" });
        }
      }

      return updated;
    });

    fetch(`/api/sessions/${sessionId}`, { method: "DELETE" }).catch(() => {});
  }

  function switchToSession(sessionId: string) {
    if (appState === "running") {
      abortRef.current?.abort();
    }

    setActiveSessionId(sessionId);
    setInput("");
    setAppState("idle");
    setQueuedMessage(null);
    connectToStream(sessionId);

    setTimeout(() => inputRef.current?.focus(), 50);
  }

  const sendMessage = useCallback(
    async (text: string) => {
      if (!text.trim()) return;

      let sessionId = activeSessionId;
      if (!sessionId) {
        const res = await fetch("/api/sessions", { method: "POST" });
        const data = await res.json();
        sessionId = data.session_id as string;
        const newSession: SessionTab = { sessionId, preview: text.slice(0, 80), messageCount: 0 };
        setSessions((prev) => [newSession, ...prev]);
        setActiveSessionId(sessionId);
      }

      // Optimistic: show user message immediately (deduped when event arrives)
      dispatch({ type: "USER_MESSAGE", content: text });
      // Optimistic: show thinking indicator while waiting for first event
      dispatch({ type: "THINKING_START" });
      setInput("");
      setAppState("sending");

      // Update sidebar preview if this is the first user message
      if (chatState.messages.filter((m) => m.role === "user").length === 0) {
        setSessions((prev) =>
          prev.map((s) =>
            s.sessionId === sessionId
              ? { ...s, preview: text.slice(0, 80), messageCount: s.messageCount + 1 }
              : s
          )
        );
      }

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        const res = await fetch(`/api/sessions/${sessionId}/run`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text }),
          signal: controller.signal,
        });

        if (!res.ok) throw new Error(`HTTP ${res.status}`);

        consumeSSEStream(res.body!.getReader());
      } catch (err: unknown) {
        if (err instanceof Error && err.name === "AbortError") {
          setAppState("idle");
        } else {
          setAppState("error");
        }
      }
    },
    [activeSessionId, chatState.messages]
  );

  // Process a queued message after turn completes
  useEffect(() => {
    if (appState === "idle" && queuedMessage) {
      const msg = queuedMessage;
      setQueuedMessage(null);
      sendMessage(msg);
    }
  }, [appState, queuedMessage, sendMessage]);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!input.trim()) return;

    if (appState === "running") {
      setQueuedMessage(input);
      setInput("");
      return;
    }

    sendMessage(input);
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape" && appState === "running") {
      abortRef.current?.abort();
      fetch(`/api/sessions/${activeSessionId}/interrupt`, { method: "POST" });
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  }

  function handlePromptClick(prompt: string) {
    sendMessage(prompt);
  }

  const isEmptyChat = chatState.messages.length === 0 && chatState.currentTurn.steps.length === 0;

  return (
    <div className="flex h-screen">
      <Sidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        onSelectSession={switchToSession}
        onNewSession={createNewSession}
        onDeleteSession={deleteSession}
      />

      <div className="flex flex-col flex-1 min-w-0">
        {/* Header */}
        <header className="px-6 py-4 flex items-center gap-2 border-b border-border">
          <span className="text-accent text-lg">&#9670;</span>
          <h1 className="text-base font-semibold text-gray-200">Data Analyst</h1>
        </header>

        {/* Messages */}
        <main className="flex-1 overflow-y-auto px-6 pb-4">
          <div className="max-w-[800px] mx-auto">
            {isEmptyChat && (
              <div className="flex flex-col items-center justify-center h-[calc(100vh-140px)] gap-6">
                <p className="text-gray-500 text-sm">Ask anything about the Chinook music store database</p>
                <div className="flex flex-wrap justify-center gap-2">
                  {SUGGESTED_PROMPTS.map((prompt) => (
                    <button
                      key={prompt}
                      onClick={() => handlePromptClick(prompt)}
                      className="px-3 py-1.5 text-[13px] text-gray-400 border border-border rounded-full hover:bg-surface transition-colors"
                    >
                      {prompt}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {chatState.messages.map((msg, i) => {
              if (msg.role === "user") {
                return <UserMessage key={i} content={msg.content!} />;
              }
              return <AgentMessage key={i} steps={msg.steps!} />;
            })}

            {/* Live agent turn */}
            {chatState.currentTurn.steps.length > 0 && (
              <AgentMessage steps={chatState.currentTurn.steps} />
            )}

            <div ref={messagesEndRef} />
          </div>
        </main>

        {/* Input */}
        <div className="px-6 pb-6 pt-2 max-w-[800px] mx-auto w-full">
          <form onSubmit={handleSubmit} className="relative">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={
                appState === "running"
                  ? "Type to steer the agent or queue a follow-up"
                  : "Ask anything..."
              }
              rows={1}
              className="w-full bg-surface text-sm text-gray-200 placeholder-gray-500 rounded-xl px-4 py-3 pr-12 resize-none focus:outline-none focus:ring-1 focus:ring-accent/50 border border-border"
            />
            <button
              type="submit"
              disabled={!input.trim() || appState === "sending"}
              className="absolute right-2 top-1/2 -translate-y-1/2 w-8 h-8 rounded-full bg-accent text-white flex items-center justify-center disabled:opacity-40 hover:bg-accent-hover transition-colors"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z" />
              </svg>
            </button>
          </form>
          {appState === "running" && (
            <p className="text-[11px] text-gray-500 mt-1.5 text-center">
              Esc to interrupt
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
