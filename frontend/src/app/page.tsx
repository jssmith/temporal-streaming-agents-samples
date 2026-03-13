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

type AppState = "idle" | "sending" | "running" | "complete" | "error";

// --- localStorage persistence ---

const STORAGE_KEY_SESSIONS = "analytics-sessions";
const STORAGE_KEY_MESSAGES = "analytics-messages";

function loadSessions(): SessionTab[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(STORAGE_KEY_SESSIONS);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function saveSessions(sessions: SessionTab[]) {
  localStorage.setItem(STORAGE_KEY_SESSIONS, JSON.stringify(sessions));
}

function loadMessages(sessionId: string): ChatMessage[] {
  try {
    const raw = localStorage.getItem(`${STORAGE_KEY_MESSAGES}:${sessionId}`);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function saveMessages(sessionId: string, messages: ChatMessage[]) {
  localStorage.setItem(`${STORAGE_KEY_MESSAGES}:${sessionId}`, JSON.stringify(messages));
}

function deleteMessages(sessionId: string) {
  localStorage.removeItem(`${STORAGE_KEY_MESSAGES}:${sessionId}`);
}

// --- Reducer for agent turn state ---

interface TurnState {
  steps: Step[];
  thinkingCounter: number;
}

type TurnAction =
  | { type: "THINKING_START" }
  | { type: "THINKING_DELTA"; delta: string }
  | { type: "THINKING_COMPLETE"; content: string }
  | { type: "TOOL_CALL_START"; callId: string; toolName: string; arguments: Record<string, unknown> }
  | { type: "TOOL_CALL_COMPLETE"; callId: string; toolName: string; result?: Record<string, unknown>; error?: string }
  | { type: "TEXT_DELTA"; delta: string }
  | { type: "TEXT_COMPLETE"; text: string }
  | { type: "RESET" };

function turnReducer(state: TurnState, action: TurnAction): TurnState {
  const steps = [...state.steps];

  switch (action.type) {
    case "THINKING_START": {
      // Reuse existing active thinking step (created optimistically on send)
      const hasActive = steps.some(
        (s) => s.type === "thinking" && s.data.status === "active"
      );
      if (hasActive) return state;
      const id = `t${state.thinkingCounter}`;
      steps.push({ type: "thinking", data: { id, status: "active", content: "" } });
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
          steps[i] = {
            ...step,
            data: { ...step.data, status: "done", content: action.content },
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
        steps[idx] = {
          ...step,
          data: {
            ...step.data,
            status: action.error ? "error" : "done",
            result: action.result,
            error: action.error,
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
    case "RESET":
      return { steps: [], thinkingCounter: 0 };
    default:
      return state;
  }
}

// --- Main Page ---

export default function Home() {
  const [sessions, setSessions] = useState<SessionTab[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [appState, setAppState] = useState<AppState>("idle");
  const [queuedMessage, setQueuedMessage] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const initializedRef = useRef(false);

  const [turnState, dispatchTurn] = useReducer(turnReducer, {
    steps: [],
    thinkingCounter: 0,
  });

  // Auto-scroll
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, turnState.steps]);

  // Restore sessions from localStorage on mount
  useEffect(() => {
    if (initializedRef.current) return;
    initializedRef.current = true;

    const saved = loadSessions();
    if (saved.length > 0) {
      setSessions(saved);
      const firstId = saved[0].sessionId;
      setActiveSessionId(firstId);
      setMessages(loadMessages(firstId));

      // Ensure backend has these sessions (they may have been lost on backend restart)
      for (const s of saved) {
        fetch(`/api/sessions/${s.sessionId}`).then((res) => {
          if (res.status === 404) {
            // Re-create on backend
            fetch("/api/sessions", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ session_id: s.sessionId }),
            });
          }
        });
      }
    }
  }, []);

  // Persist sessions to localStorage whenever they change
  useEffect(() => {
    saveSessions(sessions);
  }, [sessions]);

  // Persist messages whenever they change
  useEffect(() => {
    if (activeSessionId && messages.length > 0) {
      saveMessages(activeSessionId, messages);
    }
  }, [messages, activeSessionId]);

  function createNewSession() {
    // Save current session's messages before switching away
    if (activeSessionId && messages.length > 0) {
      saveMessages(activeSessionId, messages);
    }

    // Abort any in-flight request
    if (appState === "running") {
      abortRef.current?.abort();
    }

    // Just clear to the welcome state — backend session created lazily on first message
    setActiveSessionId(null);
    setMessages([]);
    setInput("");
    setAppState("idle");
    setQueuedMessage(null);
    dispatchTurn({ type: "RESET" });

    setTimeout(() => inputRef.current?.focus(), 50);
  }

  function deleteSession(sessionId: string) {
    setSessions((prev) => {
      const updated = prev.filter((s) => s.sessionId !== sessionId);
      saveSessions(updated);

      // If we're deleting the active session, switch to another or clear
      if (sessionId === activeSessionId) {
        if (updated.length > 0) {
          switchToSession(updated[0].sessionId);
        } else {
          setActiveSessionId(null);
          setMessages([]);
        }
      }

      return updated;
    });

    deleteMessages(sessionId);
  }

  function switchToSession(sessionId: string) {
    // Save current session's messages
    if (activeSessionId && messages.length > 0) {
      saveMessages(activeSessionId, messages);
    }

    // Abort any in-flight request
    if (appState === "running") {
      abortRef.current?.abort();
    }

    setActiveSessionId(sessionId);
    setMessages(loadMessages(sessionId));
    setInput("");
    setAppState("idle");
    setQueuedMessage(null);
    dispatchTurn({ type: "RESET" });

    // Focus input
    setTimeout(() => inputRef.current?.focus(), 50);
  }

  // Keep sidebar preview in sync with messages
  function updateSessionPreview(sessionId: string, firstUserMessage: string) {
    setSessions((prev) => {
      const updated = prev.map((s) =>
        s.sessionId === sessionId
          ? { ...s, preview: firstUserMessage.slice(0, 80), messageCount: s.messageCount + 1 }
          : s
      );
      saveSessions(updated);
      return updated;
    });
  }

  const sendMessage = useCallback(
    async (text: string) => {
      if (!text.trim()) return;

      // Auto-create a session if none is active
      let sessionId = activeSessionId;
      if (!sessionId) {
        const res = await fetch("/api/sessions", { method: "POST" });
        const data = await res.json();
        sessionId = data.session_id as string;
        const newSession = { sessionId, preview: text.slice(0, 80), messageCount: 0 };
        setSessions((prev) => {
          const updated = [newSession, ...prev];
          saveSessions(updated);
          return updated;
        });
        setActiveSessionId(sessionId);
      }

      const newMessages: ChatMessage[] = [...messages, { role: "user", content: text }];
      setMessages(newMessages);
      setInput("");
      setAppState("sending");
      dispatchTurn({ type: "RESET" });
      // Show thinking indicator immediately while waiting for SSE
      dispatchTurn({ type: "THINKING_START" });

      // Update preview if this is the first user message
      const userMsgCount = newMessages.filter((m) => m.role === "user").length;
      if (userMsgCount === 1) {
        updateSessionPreview(sessionId, text);
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

        setAppState("running");

        const reader = res.body!.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n\n");
          buffer = lines.pop() || "";

          for (const chunk of lines) {
            if (!chunk.startsWith("data: ")) continue;
            const jsonStr = chunk.slice(6);
            try {
              const event: SSEEvent = JSON.parse(jsonStr);
              processEvent(event);
            } catch {
              // skip malformed events
            }
          }
        }

        setAppState("complete");
      } catch (err: unknown) {
        if (err instanceof Error && err.name === "AbortError") {
          setAppState("idle");
        } else {
          setAppState("error");
        }
      }
    },
    [activeSessionId, messages]
  );

  // Process a queued message after turn completes
  useEffect(() => {
    if (appState === "complete" && queuedMessage) {
      const msg = queuedMessage;
      setQueuedMessage(null);
      sendMessage(msg);
    }
  }, [appState, queuedMessage, sendMessage]);

  // Snapshot agent turn into messages when complete
  useEffect(() => {
    if (
      (appState === "complete" || appState === "idle") &&
      turnState.steps.length > 0
    ) {
      setMessages((prev) => {
        const updated = [...prev, { role: "agent" as const, steps: [...turnState.steps] }];
        if (activeSessionId) {
          saveMessages(activeSessionId, updated);
        }
        return updated;
      });
      dispatchTurn({ type: "RESET" });
    }
  }, [appState, turnState.steps, activeSessionId]);

  function processEvent(event: SSEEvent) {
    const d = event.data;
    switch (event.type) {
      case "THINKING_START":
        dispatchTurn({ type: "THINKING_START" });
        break;
      case "THINKING_DELTA":
        dispatchTurn({ type: "THINKING_DELTA", delta: d.delta as string });
        break;
      case "THINKING_COMPLETE":
        dispatchTurn({ type: "THINKING_COMPLETE", content: d.content as string });
        break;
      case "TOOL_CALL_START":
        dispatchTurn({
          type: "TOOL_CALL_START",
          callId: d.call_id as string,
          toolName: d.tool_name as string,
          arguments: d.arguments as Record<string, unknown>,
        });
        break;
      case "TOOL_CALL_COMPLETE":
        dispatchTurn({
          type: "TOOL_CALL_COMPLETE",
          callId: d.call_id as string,
          toolName: d.tool_name as string,
          result: d.result as Record<string, unknown> | undefined,
          error: d.error as string | undefined,
        });
        break;
      case "TEXT_DELTA":
        dispatchTurn({ type: "TEXT_DELTA", delta: d.delta as string });
        break;
      case "TEXT_COMPLETE":
        dispatchTurn({ type: "TEXT_COMPLETE", text: d.text as string });
        break;
    }
  }

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

  const isEmptyChat = messages.length === 0 && turnState.steps.length === 0;

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

            {messages.map((msg, i) => {
              if (msg.role === "user") {
                return <UserMessage key={i} content={msg.content!} />;
              }
              return <AgentMessage key={i} steps={msg.steps!} />;
            })}

            {/* Live agent turn */}
            {turnState.steps.length > 0 && <AgentMessage steps={turnState.steps} />}

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
