"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { UserMessage, AgentMessage } from "./components/ChatMessage";
import Sidebar, { SessionTab } from "./components/Sidebar";
import { useSessionRuntimes } from "./useSessionRuntimes";

const SUGGESTED_PROMPTS = [
  "Build a bar chart of the top 10 genres by revenue",
  "Which genres are most popular by country? Show a pivot table",
  "Compare monthly revenue trends across years using pandas",
  "Run a customer segmentation analysis with spending tiers",
  "Find the top 5 artists by track count and revenue side by side",
];

// Delay before moving focus back to the input after a session switch or new
// chat, so the textarea is mounted/painted first.
const FOCUS_DELAY_MS = 50;

// --- Main Page ---

export default function Home() {
  const [sessions, setSessions] = useState<SessionTab[]>([]);
  const [input, setInput] = useState("");
  const [queuedMessage, setQueuedMessage] = useState<string | null>(null);

  // Sidebar/input side effects that sendMessage must trigger at exact points
  // in its flow. Kept here so the runtime hook stays focused on streaming.
  const onSessionCreated = useCallback((sessionId: string, text: string) => {
    const newSession: SessionTab = { sessionId, preview: text.slice(0, 80), messageCount: 0 };
    setSessions(prev => [newSession, ...prev]);
  }, []);

  const onUserMessageSent = useCallback(
    (sessionId: string, text: string, isFirstUserMessage: boolean) => {
      setInput("");
      if (isFirstUserMessage) {
        setSessions(prev =>
          prev.map(s =>
            s.sessionId === sessionId
              ? { ...s, preview: text.slice(0, 80), messageCount: s.messageCount + 1 }
              : s
          )
        );
      }
    },
    [],
  );

  const {
    activeSessionId,
    runtimesRef,
    chatState,
    appState,
    isSessionLoading,
    setActive,
    setRuntimes,
    updateRuntime,
    ensureSessionStream,
    startLoadingIndicator,
    clearSessionLoading,
    sendMessage,
    interruptActive,
  } = useSessionRuntimes({ onSessionCreated, onUserMessageSent });

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const previousActiveSessionIdRef = useRef<string | null>(null);

  // --- Initial session list -------------------------------------------------

  useEffect(() => {
    fetch("/api/sessions")
      .then(res => res.json())
      .then((data: { session_id: string; message_count: number; preview: string }[]) => {
        const tabs: SessionTab[] = data.map(s => ({
          sessionId: s.session_id,
          preview: s.preview,
          messageCount: s.message_count,
        }));
        setSessions(tabs);
        if (tabs.length > 0) {
          const first = tabs[0].sessionId;
          setActive(first);
          ensureSessionStream(first);
          startLoadingIndicator(first);
        }
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- Scroll: instant on session switch, smooth on incoming content -------

  useEffect(() => {
    const previous = previousActiveSessionIdRef.current;
    previousActiveSessionIdRef.current = activeSessionId;
    if (!messagesEndRef.current) return;
    const sessionSwitched = previous !== activeSessionId;
    messagesEndRef.current.scrollIntoView({
      behavior: sessionSwitched ? "instant" : "smooth",
    });
  }, [activeSessionId, chatState.messages.length, chatState.currentTurn.steps.length]);

  // --- Session management ---------------------------------------------------

  function createNewSession() {
    setActive(null);
    setInput("");
    setQueuedMessage(null);
    clearSessionLoading();
    setTimeout(() => inputRef.current?.focus(), FOCUS_DELAY_MS);
  }

  function deleteSession(sessionId: string) {
    setSessions(prev => {
      const updated = prev.filter(s => s.sessionId !== sessionId);
      if (sessionId === activeSessionId) {
        if (updated.length > 0) {
          const next = updated[0].sessionId;
          setActive(next);
          ensureSessionStream(next);
          startLoadingIndicator(next);
        } else {
          setActive(null);
        }
      }
      return updated;
    });
    // Drop the runtime and abort any in-flight stream for the deleted session.
    setRuntimes(prev => {
      const rt = prev.get(sessionId);
      rt?.controller?.abort();
      const next = new Map(prev);
      next.delete(sessionId);
      return next;
    });
    fetch(`/api/sessions/${sessionId}`, { method: "DELETE" }).catch(() => {});
  }

  function switchToSession(sessionId: string) {
    setActive(sessionId);
    setInput("");
    setQueuedMessage(null);
    // Touch the LRU if cached. ensureSessionStream is idempotent — it skips
    // sessions with an in-flight controller or cached content, but re-opens
    // a runtime that exists with empty state and no controller (e.g. left
    // over from an earlier failed fetch).
    if (runtimesRef.current.has(sessionId)) {
      updateRuntime(sessionId, current => current);
    }
    ensureSessionStream(sessionId);
    startLoadingIndicator(sessionId);
    setTimeout(() => inputRef.current?.focus(), FOCUS_DELAY_MS);
  }

  // Process a queued message after the active session's turn completes.
  useEffect(() => {
    if (appState === "idle" && queuedMessage) {
      const msg = queuedMessage;
      setQueuedMessage(null);
      sendMessage(msg);
    }
  }, [appState, queuedMessage, sendMessage]);

  // Esc interrupts the running turn regardless of focus. Binding to the
  // textarea alone misses the common case where the user has clicked a step,
  // scrolled the transcript, or touched the sidebar while the turn streams.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") interruptActive();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [interruptActive]);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!input.trim()) return;

    // Queue while a turn is in flight (including the brief "sending" window
    // before the first event) so Enter can't fire a second /run for the same
    // session. The queued message is sent once the active turn goes idle.
    if (appState === "sending" || appState === "running" || appState === "interrupting") {
      setQueuedMessage(input);
      setInput("");
      return;
    }

    sendMessage(input);
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    // Esc is handled by a window-level listener (see interruptActive) so it
    // works regardless of focus. Here we only handle Enter-to-send.
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  }

  function handlePromptClick(prompt: string) {
    sendMessage(prompt);
  }

  const isEmptyChat = chatState.messages.length === 0 && chatState.currentTurn.steps.length === 0;
  // Suggested-prompts picker is only meaningful for a brand-new chat
  // (no session selected yet). Switching between existing sessions
  // shows a blank canvas during any load gap.
  const showSuggestedPrompts = isEmptyChat && activeSessionId === null;

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
            {isSessionLoading && isEmptyChat && !showSuggestedPrompts && (
              <div className="flex flex-col items-center justify-center h-[calc(100vh-140px)]">
                <div className="text-gray-500 text-sm animate-pulse">Loading conversation…</div>
              </div>
            )}

            {showSuggestedPrompts && (
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
                return <UserMessage key={i} content={msg.content} />;
              }
              return <AgentMessage key={i} steps={msg.steps} interrupted={msg.interrupted} />;
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
                appState === "running" || appState === "interrupting"
                  ? "Type to steer the agent or queue a follow-up"
                  : "Ask anything..."
              }
              rows={1}
              className="w-full bg-surface text-sm text-gray-200 placeholder-gray-500 rounded-xl px-4 py-3 pr-12 resize-none focus:outline-none focus:ring-1 focus:ring-accent/50 border border-border"
            />
            <button
              type="submit"
              disabled={!input.trim() || appState === "sending"}
              aria-label="Send message"
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
          {appState === "interrupting" && (
            <p className="text-[11px] text-gray-500 mt-1.5 text-center animate-pulse">
              Stopping…
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
