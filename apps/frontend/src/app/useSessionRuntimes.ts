"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import {
  chatReducer,
  initialChatState,
  ChatState,
  ChatAction,
  SSEEvent,
} from "../lib/chatReducer";
import { processEvent, AppState } from "../lib/processEvent";

// Up to this many recent sessions are kept hot in memory. Streams stay open
// for in-flight turns even on background tabs, so flipping back to a tab is
// instant and any progress that arrived while you were elsewhere is already
// applied. Older sessions are evicted (their streams aborted).
const MAX_CACHED_SESSIONS = 5;

// Loading indicator shows only if a fresh stream takes longer than this to
// deliver its first event. Cached restores never show it.
const LOADING_INDICATOR_DELAY_MS = 250;

// Per-session runtime: the cached chat state, its appState, and (if a stream
// is currently open) an AbortController for the in-flight /run or /stream
// fetch. A session has at most one open stream at a time — sending a new
// message aborts the prior one before opening /run.
export type SessionRuntime = {
  chatState: ChatState;
  appState: AppState;
  controller: AbortController | null;
};

const newRuntime = (): SessionRuntime => ({
  chatState: initialChatState,
  appState: "idle",
  controller: null,
});

function hasContent(rt: SessionRuntime | undefined): boolean {
  return (
    rt !== undefined &&
    (rt.chatState.messages.length > 0 ||
      rt.chatState.currentTurn.steps.length > 0)
  );
}

// Evict least-recently-used runtimes (aborting their streams) until the map
// has room for one more entry, never evicting `keepId`. Mutates and returns
// `next`. The map is ordered most-recently-used last, so the first key is
// the eviction target.
function evictToFit(next: Map<string, SessionRuntime>, keepId?: string) {
  while (next.size >= MAX_CACHED_SESSIONS && !(keepId && next.has(keepId))) {
    const oldest = next.keys().next().value;
    if (oldest === undefined) break;
    if (oldest === keepId) break;
    next.get(oldest)?.controller?.abort();
    next.delete(oldest);
  }
}

// Sidebar/input side effects that live in Home() but must fire at precise
// points inside sendMessage's flow. Kept as callbacks so the hook owns the
// streaming/runtime work without reaching into Home()'s `sessions`/`input`
// state.
export type SessionRuntimesCallbacks = {
  // A brand-new session was just created for this send; prepend it to the
  // sidebar with this preview text.
  onSessionCreated: (sessionId: string, text: string) => void;
  // A user message was just dispatched. `isFirstUserMessage` is true when
  // this is the first user turn in the session (drives the preview refresh).
  onUserMessageSent: (
    sessionId: string,
    text: string,
    isFirstUserMessage: boolean,
  ) => void;
};

// What Home() needs from the streaming runtime layer.
export type SessionRuntimes = {
  activeSessionId: string | null;
  activeSessionIdRef: React.MutableRefObject<string | null>;
  runtimesRef: React.MutableRefObject<Map<string, SessionRuntime>>;
  chatState: ChatState;
  appState: AppState;
  isSessionLoading: boolean;
  setActive: (id: string | null) => void;
  setRuntimes: React.Dispatch<React.SetStateAction<Map<string, SessionRuntime>>>;
  updateRuntime: (
    sessionId: string,
    updater: (current: SessionRuntime) => SessionRuntime | undefined,
  ) => void;
  ensureSessionStream: (sessionId: string) => void;
  startLoadingIndicator: (sessionId: string) => void;
  clearSessionLoading: () => void;
  seedRuntime: (sessionId: string) => void;
  sendMessage: (text: string) => Promise<void>;
  interruptActive: () => void;
};

// Owns the per-session runtime map and everything that drives the background
// SSE streams: the LRU cache, the loading-indicator timer, the dispatch/
// appState setters, and the /stream + /run + interrupt lifecycle. This is the
// centerpiece of the sample — one named unit so the streaming pattern reads
// cleanly out of Home().
export function useSessionRuntimes(
  callbacks: SessionRuntimesCallbacks,
): SessionRuntimes {
  // Mirror callbacks in a ref so sendMessage can stay referentially stable
  // (depend only on []-stable values) while still calling the latest Home()
  // closures.
  const callbacksRef = useRef(callbacks);
  useEffect(() => {
    callbacksRef.current = callbacks;
  }, [callbacks]);

  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);

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

  // Mirror activeSessionId so async stream callbacks see the current value
  // instead of the one captured when the callback was created. Critically,
  // we update the ref synchronously inside setActive() rather than via a
  // useEffect — a useEffect lags by one render commit, leaving a same-tick
  // window where a fast stream failure right after a session switch could
  // still see the old id and miss the loading-indicator clear.
  const activeSessionIdRef = useRef<string | null>(null);
  const setActive = useCallback((id: string | null) => {
    activeSessionIdRef.current = id;
    setActiveSessionId(id);
  }, []);

  // Loading indicator (shown if a fresh stream takes longer than
  // LOADING_INDICATOR_DELAY_MS to deliver its first event). Cached restores
  // never show it.
  const [isSessionLoading, setIsSessionLoading] = useState(false);
  const sessionLoadingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearSessionLoading = useCallback(() => {
    if (sessionLoadingTimerRef.current) {
      clearTimeout(sessionLoadingTimerRef.current);
      sessionLoadingTimerRef.current = null;
    }
    setIsSessionLoading(false);
  }, []);

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
  const updateRuntime = useCallback(
    (
      sessionId: string,
      updater: (current: SessionRuntime) => SessionRuntime | undefined,
    ) => {
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
    },
    [],
  );

  const dispatchToSession = useCallback(
    (sessionId: string, action: ChatAction) => {
      updateRuntime(sessionId, current => ({
        ...current,
        chatState: chatReducer(current.chatState, action),
      }));
    },
    [updateRuntime],
  );

  const setAppStateFor = useCallback(
    (sessionId: string, nextAppState: AppState) => {
      updateRuntime(sessionId, current => ({ ...current, appState: nextAppState }));
    },
    [updateRuntime],
  );

  // Shared stream-teardown: an AbortError means an intentional cancel (drop
  // back to idle), anything else is a real failure (surface "error"). Either
  // way clear the loading indicator if this is the active session and drop
  // the controller so a later send doesn't abort a finished fetch.
  const handleStreamError = useCallback(
    (sessionId: string, err: unknown) => {
      if (sessionId === activeSessionIdRef.current) clearSessionLoading();
      if (err instanceof Error && err.name === "AbortError") {
        setAppStateFor(sessionId, "idle");
      } else {
        setAppStateFor(sessionId, "error");
      }
      updateRuntime(sessionId, current => ({ ...current, controller: null }));
    },
    [clearSessionLoading, setAppStateFor, updateRuntime],
  );

  // --- SSE consumption ------------------------------------------------------

  const consumeSSEStream = useCallback(
    (sessionId: string, reader: ReadableStreamDefaultReader<Uint8Array>) => {
      const decoder = new TextDecoder();
      let buffer = "";

      // Parse a single "\n\n"-delimited SSE block: gather its `data:` lines
      // (concatenated for multi-line data), ignore comment lines (": ...")
      // and other fields, then JSON.parse and dispatch. Malformed JSON is
      // skipped.
      const handleChunk = (chunk: string) => {
        const dataParts: string[] = [];
        for (const line of chunk.split("\n")) {
          if (line.startsWith("data:")) {
            dataParts.push(line.slice(line.startsWith("data: ") ? 6 : 5));
          }
          // ": ..." comment lines and other SSE fields (event:, id:, retry:)
          // are ignored.
        }
        if (dataParts.length === 0) return;
        try {
          const event: SSEEvent = JSON.parse(dataParts.join("\n"));
          processEvent(
            event,
            action => dispatchToSession(sessionId, action),
            state => setAppStateFor(sessionId, state),
          );
        } catch {
          // skip malformed events
        }
      };

      (async () => {
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const blocks = buffer.split("\n\n");
            buffer = blocks.pop() || "";
            for (const block of blocks) handleChunk(block);
          }
          // Flush any bytes the decoder is still holding, then parse a final
          // block that arrived without a trailing "\n\n" so a terminating
          // frame can't strand the turn "live".
          buffer += decoder.decode();
          if (buffer.trim().length > 0) handleChunk(buffer);

          setAppStateFor(sessionId, "idle");
          if (sessionId === activeSessionIdRef.current) clearSessionLoading();
        } catch (err: unknown) {
          handleStreamError(sessionId, err);
          return;
        }
        // Stream finished cleanly; clear the controller so a future send
        // doesn't try to abort an already-finished fetch.
        updateRuntime(sessionId, current => ({ ...current, controller: null }));
      })();
    },
    [
      dispatchToSession,
      setAppStateFor,
      clearSessionLoading,
      handleStreamError,
      updateRuntime,
    ],
  );

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
  const ensureSessionStream = useCallback(
    (sessionId: string) => {
      const existing = runtimesRef.current.get(sessionId);
      if (existing?.controller) return; // stream already in flight
      if (hasContent(existing)) return; // already populated; no need to re-stream

      const controller = new AbortController();
      const claim = (prev: Map<string, SessionRuntime>) => {
        const cur = prev.get(sessionId);
        // If a different controller has already been installed (e.g.
        // sendMessage's queued updater landed in this batch), leave it alone.
        if (cur?.controller && cur.controller !== controller) return prev;
        const next = new Map(prev);
        evictToFit(next, sessionId);
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
        .catch(err => handleStreamError(sessionId, err));
    },
    [consumeSSEStream, handleStreamError],
  );

  // --- Loading indicator ---------------------------------------------------

  // Only schedule the indicator when the session being switched to has no
  // content yet. Cached sessions paint instantly and never need it.
  const startLoadingIndicator = useCallback(
    (sessionId: string) => {
      clearSessionLoading();
      if (hasContent(runtimesRef.current.get(sessionId))) return;
      sessionLoadingTimerRef.current = setTimeout(
        () => setIsSessionLoading(true),
        LOADING_INDICATOR_DELAY_MS,
      );
    },
    [clearSessionLoading],
  );

  // Defensive auto-clear: if content starts flowing, hide the indicator
  // even if the timeout already fired.
  useEffect(() => {
    if (chatState.messages.length > 0 || chatState.currentTurn.steps.length > 0) {
      clearSessionLoading();
    }
  }, [
    activeSessionId,
    chatState.messages.length,
    chatState.currentTurn.steps.length,
    clearSessionLoading,
  ]);

  // Seed an empty runtime for a brand-new session so optimistic dispatches
  // have something to update. Evicts to fit, never the new session itself.
  const seedRuntime = useCallback((sessionId: string) => {
    setRuntimes(prev => {
      const next = new Map(prev);
      evictToFit(next, sessionId);
      next.set(sessionId, newRuntime());
      return next;
    });
  }, []);

  const sendMessage = useCallback(
    async (text: string) => {
      if (!text.trim()) return;

      let sessionId = activeSessionIdRef.current;
      if (!sessionId) {
        const res = await fetch("/api/sessions", { method: "POST" });
        const data = await res.json();
        sessionId = data.session_id as string;
        callbacksRef.current.onSessionCreated(sessionId, text);
        setActive(sessionId);
        // Seed an empty runtime so the dispatches below have something to update.
        seedRuntime(sessionId);
      }
      const targetSessionId = sessionId;

      // Abort any /stream that was open for this session before /run takes
      // over. Two streams subscribing from the same offset would deliver
      // duplicates.
      const existing = runtimesRef.current.get(targetSessionId);
      existing?.controller?.abort();

      // Whether this is the session's first user message — drives the sidebar
      // preview refresh. Read before the optimistic dispatch lands (setState
      // is queued, so runtimesRef still holds the pre-send state).
      const cached = runtimesRef.current.get(targetSessionId);
      const isFirstUserMessage = cached
        ? cached.chatState.messages.filter(m => m.role === "user").length === 0
        : true;

      // Optimistic: show user message + thinking indicator immediately.
      dispatchToSession(targetSessionId, { type: "USER_MESSAGE", content: text });
      dispatchToSession(targetSessionId, { type: "THINKING_START" });
      setAppStateFor(targetSessionId, "sending");
      callbacksRef.current.onUserMessageSent(targetSessionId, text, isFirstUserMessage);

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
        handleStreamError(targetSessionId, err);
      }
    },
    [
      setActive,
      seedRuntime,
      dispatchToSession,
      setAppStateFor,
      updateRuntime,
      consumeSSEStream,
      handleStreamError,
    ],
  );

  // Interrupt the active turn with immediate UI acknowledgement. Reads live
  // state from refs (not closure) so it's safe to call from a long-lived
  // window listener. The setters operate via setRuntimes, so a stable []
  // dependency list is correct.
  const interruptActive = useCallback(() => {
    const sid = activeSessionIdRef.current;
    if (!sid) return;
    const runtime = runtimesRef.current.get(sid);
    if (!runtime || runtime.appState !== "running") return; // nothing to stop
    setAppStateFor(sid, "interrupting");
    fetch(`/api/sessions/${sid}/interrupt`, { method: "POST" })
      .then(res => {
        if (!res.ok) throw new Error(`interrupt failed: ${res.status}`);
      })
      .catch(() => {
        runtimesRef.current.get(sid)?.controller?.abort();
      });
  }, [setAppStateFor]);

  // Component-unmount cleanup: abort every active stream so nothing leaks,
  // and cancel a pending loading-indicator timer.
  useEffect(() => {
    return () => {
      runtimesRef.current.forEach(rt => rt.controller?.abort());
      if (sessionLoadingTimerRef.current) clearTimeout(sessionLoadingTimerRef.current);
    };
  }, []);

  return {
    activeSessionId,
    activeSessionIdRef,
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
    seedRuntime,
    sendMessage,
    interruptActive,
  };
}
