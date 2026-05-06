"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { UserMessage, AgentMessage } from "./components/ChatMessage";
import Sidebar, { SessionTab } from "./components/Sidebar";
import { chatReducer, initialChatState, ChatState, ChatAction, SSEEvent } from "../lib/chatReducer";
import { processEvent, AppState } from "../lib/processEvent";

const SUGGESTED_PROMPTS = [
  "Build a bar chart of the top 10 genres by revenue",
  "Which genres are most popular by country? Show a pivot table",
  "Compare monthly revenue trends across years using pandas",
  "Run a customer segmentation analysis with spending tiers",
  "Find the top 5 artists by track count and revenue side by side",
];

// Up to this many recent sessions are kept hot in memory. Streams stay open
// for in-flight turns even on background tabs, so flipping back to a tab is
// instant and any progress that arrived while you were elsewhere is already
// applied. Older sessions are evicted (their streams aborted).
const MAX_CACHED_SESSIONS = 5;

// Per-session runtime: the cached chat state, its appState, and (if a stream
// is currently open) an AbortController for the in-flight /run or /stream
// fetch. A session has at most one open stream at a time — sending a new
// message aborts the prior one before opening /run.
type SessionRuntime = {
  chatState: ChatState;
  appState: AppState;
  controller: AbortController | null;
};

const newRuntime = (): SessionRuntime => ({
  chatState: initialChatState,
  appState: "idle",
  controller: null,
});

// --- Main Page ---

export default function Home() {
  const [sessions, setSessions] = useState<SessionTab[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [queuedMessage, setQueuedMessage] = useState<string | null>(null);

  // Map keyed by sessionId, ordered most-recently-used last so the first
  // entry is the eviction target.
  const [runtimes, setRuntimes] = useState<Map<string, SessionRuntime>>(
    () => new Map(),
  );
  // Mirror of `runtimes` that callbacks can read synchronously without
  // depending on stale closure values.
  const runtimesRef = useRef(runtimes);
  useEffect(() => {
    runtimesRef.current = runtimes;
  }, [runtimes]);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const previousActiveSessionIdRef = useRef<string | null>(null);
  // Mirror activeSessionId so async stream callbacks see the current value
  // instead of the one captured when the callback was created. Critically,
  // we update the ref synchronously inside setActive() rather than via a
  // useEffect — a useEffect lags by one render commit, leaving a same-tick
  // window where a fast stream failure right after a session switch could
  // still see the old id and miss the loading-indicator clear.
  const activeSessionIdRef = useRef<string | null>(null);
  function setActive(id: string | null) {
    activeSessionIdRef.current = id;
    setActiveSessionId(id);
  }

  // Loading indicator (shown if a fresh stream takes longer than ~250 ms
  // to deliver its first event). Cached restores never show it.
  const [isSessionLoading, setIsSessionLoading] = useState(false);
  const sessionLoadingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  function clearSessionLoading() {
    if (sessionLoadingTimerRef.current) {
      clearTimeout(sessionLoadingTimerRef.current);
      sessionLoadingTimerRef.current = null;
    }
    setIsSessionLoading(false);
  }

  // Active runtime drives all rendering. Defaults are "show nothing" so the
  // initial empty state and the new-chat state collapse to the same render.
  const activeRuntime = activeSessionId ? runtimes.get(activeSessionId) : undefined;
  const chatState = activeRuntime?.chatState ?? initialChatState;
  const appState = activeRuntime?.appState ?? "idle";

  // --- Per-session map updates ----------------------------------------------

  // Atomic update: read the current runtime, return a new one (or undefined
  // to delete). No-op if the session has been evicted/deleted — we don't
  // resurrect zombie runtimes from late stream-teardown callbacks. Touches
  // the LRU order by re-inserting at the end.
  function updateRuntime(
    sessionId: string,
    updater: (current: SessionRuntime) => SessionRuntime | undefined,
  ) {
    setRuntimes(prev => {
      const current = prev.get(sessionId);
      if (current === undefined) return prev;
      const result = updater(current);
      if (result === undefined) {
        const next = new Map(prev);
        next.delete(sessionId);
        return next;
      }
      const next = new Map(prev);
      next.delete(sessionId);
      next.set(sessionId, result);
      return next;
    });
  }

  function dispatchToSession(sessionId: string, action: ChatAction) {
    updateRuntime(sessionId, current => ({
      ...current,
      chatState: chatReducer(current.chatState, action),
    }));
  }

  function setAppStateFor(sessionId: string, appState: AppState) {
    updateRuntime(sessionId, current => ({ ...current, appState }));
  }

  // --- SSE consumption ------------------------------------------------------

  function consumeSSEStream(
    sessionId: string,
    reader: ReadableStreamDefaultReader<Uint8Array>,
  ) {
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
              processEvent(
                event,
                action => dispatchToSession(sessionId, action),
                state => setAppStateFor(sessionId, state),
              );
            } catch {
              // skip malformed events
            }
          }
        }
        setAppStateFor(sessionId, "idle");
        if (sessionId === activeSessionIdRef.current) clearSessionLoading();
      } catch (err: unknown) {
        if (sessionId === activeSessionIdRef.current) clearSessionLoading();
        if (err instanceof Error && err.name === "AbortError") {
          setAppStateFor(sessionId, "idle");
        } else {
          setAppStateFor(sessionId, "error");
        }
      } finally {
        // Stream is done; clear the controller so a future send doesn't try
        // to abort an already-finished fetch.
        updateRuntime(sessionId, current => ({ ...current, controller: null }));
      }
    })();
  }

  // --- Stream lifecycle -----------------------------------------------------

  // Open a /stream subscription for a session that's not yet streaming.
  // Re-opens if a prior stream was aborted before any content arrived
  // (e.g. StrictMode dev double-mount); skips if already streaming or
  // already populated.
  //
  // We can't gate the fetch on a flag set inside a setRuntimes updater —
  // React 18 batches functional updaters and runs them at render time, so
  // the flag is unreliable when read synchronously. Instead, claim the slot
  // synchronously against runtimesRef (so a same-tick caller sees us) and
  // queue a functional setRuntimes that composes with any concurrent
  // updates to the runtimes map (e.g. SSE dispatches landing in the same
  // batch).
  function ensureSessionStream(sessionId: string) {
    const existing = runtimesRef.current.get(sessionId);
    if (existing?.controller) return; // stream already in flight
    if (
      existing &&
      (existing.chatState.messages.length > 0 ||
        existing.chatState.currentTurn.steps.length > 0)
    ) {
      return; // already populated; no need to re-stream
    }

    const controller = new AbortController();
    const claim = (prev: Map<string, SessionRuntime>) => {
      const cur = prev.get(sessionId);
      // If a different controller has already been installed (e.g. sendMessage's
      // queued updater landed in this batch), leave it alone.
      if (cur?.controller && cur.controller !== controller) return prev;
      const next = new Map(prev);
      while (next.size >= MAX_CACHED_SESSIONS && !next.has(sessionId)) {
        const oldest = next.keys().next().value;
        if (oldest === undefined) break;
        next.get(oldest)?.controller?.abort();
        next.delete(oldest);
      }
      const seed = cur ?? newRuntime();
      next.delete(sessionId);
      next.set(sessionId, { ...seed, controller });
      return next;
    };
    runtimesRef.current = claim(runtimesRef.current); // sync claim
    setRuntimes(prev => claim(prev)); // composes with other queued updaters

    fetch(`/api/sessions/${sessionId}/stream?from_index=0`, {
      signal: controller.signal,
    })
      .then(res => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        consumeSSEStream(sessionId, res.body!.getReader());
      })
      .catch(err => {
        if (sessionId === activeSessionIdRef.current) clearSessionLoading();
        if (!(err instanceof Error && err.name === "AbortError")) {
          setAppStateFor(sessionId, "error");
        }
        updateRuntime(sessionId, current => ({ ...current, controller: null }));
      });
  }

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
    // Component-unmount cleanup: abort every active stream so nothing leaks.
    return () => {
      runtimesRef.current.forEach(rt => rt.controller?.abort());
      if (sessionLoadingTimerRef.current) clearTimeout(sessionLoadingTimerRef.current);
    };
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

  // --- Loading indicator ---------------------------------------------------

  // Only schedule the indicator when the session being switched to has no
  // content yet. Cached sessions paint instantly and never need it.
  function startLoadingIndicator(sessionId: string) {
    clearSessionLoading();
    const cached = runtimesRef.current.get(sessionId);
    const hasContent =
      cached &&
      (cached.chatState.messages.length > 0 ||
        cached.chatState.currentTurn.steps.length > 0);
    if (hasContent) return;
    sessionLoadingTimerRef.current = setTimeout(() => setIsSessionLoading(true), 250);
  }

  // Defensive auto-clear: if content starts flowing, hide the indicator
  // even if the timeout already fired.
  useEffect(() => {
    if (chatState.messages.length > 0 || chatState.currentTurn.steps.length > 0) {
      clearSessionLoading();
    }
  }, [activeSessionId, chatState.messages.length, chatState.currentTurn.steps.length]);

  // --- Session management ---------------------------------------------------

  function createNewSession() {
    setActive(null);
    setInput("");
    setQueuedMessage(null);
    clearSessionLoading();
    setTimeout(() => inputRef.current?.focus(), 50);
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
        setSessions(prev => [newSession, ...prev]);
        setActive(sessionId);
        // Seed an empty runtime so the dispatches below have something to update.
        setRuntimes(prev => {
          const next = new Map(prev);
          while (next.size >= MAX_CACHED_SESSIONS) {
            const oldest = next.keys().next().value;
            if (oldest === undefined) break;
            next.get(oldest)?.controller?.abort();
            next.delete(oldest);
          }
          next.set(sessionId!, newRuntime());
          return next;
        });
      }
      const targetSessionId = sessionId;

      // Abort any /stream that was open for this session before /run takes
      // over. Two streams subscribing from the same offset would deliver
      // duplicates.
      const existing = runtimesRef.current.get(targetSessionId);
      existing?.controller?.abort();

      // Optimistic: show user message + thinking indicator immediately.
      dispatchToSession(targetSessionId, { type: "USER_MESSAGE", content: text });
      dispatchToSession(targetSessionId, { type: "THINKING_START" });
      setInput("");
      setAppStateFor(targetSessionId, "sending");

      // Update sidebar preview if this is the first user message.
      const cached = runtimesRef.current.get(targetSessionId);
      const userMsgCount = cached
        ? cached.chatState.messages.filter(m => m.role === "user").length
        : 0;
      if (userMsgCount === 0) {
        setSessions(prev =>
          prev.map(s =>
            s.sessionId === targetSessionId
              ? { ...s, preview: text.slice(0, 80), messageCount: s.messageCount + 1 }
              : s
          )
        );
      }

      const controller = new AbortController();
      updateRuntime(targetSessionId, current => ({ ...current, controller }));

      try {
        const res = await fetch(`/api/sessions/${targetSessionId}/run`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text }),
          signal: controller.signal,
        });

        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        consumeSSEStream(targetSessionId, res.body!.getReader());
      } catch (err: unknown) {
        if (err instanceof Error && err.name === "AbortError") {
          setAppStateFor(targetSessionId, "idle");
        } else {
          setAppStateFor(targetSessionId, "error");
        }
        updateRuntime(targetSessionId, current => ({ ...current, controller: null }));
      }
    },
    [activeSessionId]
  );

  // Process a queued message after the active session's turn completes.
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
    if (e.key === "Escape" && appState === "running" && activeSessionId) {
      runtimesRef.current.get(activeSessionId)?.controller?.abort();
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
