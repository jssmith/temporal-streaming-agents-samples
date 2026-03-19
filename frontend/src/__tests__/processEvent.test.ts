import { describe, it, expect, vi } from "vitest";
import { processEvent, AppState } from "../lib/processEvent";
import { ChatAction, SSEEvent } from "../lib/chatReducer";

function makeEvent(type: string, data: Record<string, unknown> = {}): SSEEvent {
  return { type, timestamp: "2024-01-01T00:00:00Z", data };
}

describe("processEvent", () => {
  it("USER_MESSAGE dispatches USER_MESSAGE action", () => {
    const dispatch = vi.fn();
    const setAppState = vi.fn();
    processEvent(makeEvent("USER_MESSAGE", { content: "hi" }), dispatch, setAppState);
    expect(dispatch).toHaveBeenCalledWith({ type: "USER_MESSAGE", content: "hi" });
    expect(setAppState).not.toHaveBeenCalled();
  });

  it("AGENT_START sets appState to running", () => {
    const dispatch = vi.fn();
    const setAppState = vi.fn();
    processEvent(makeEvent("AGENT_START"), dispatch, setAppState);
    expect(setAppState).toHaveBeenCalledWith("running");
    expect(dispatch).not.toHaveBeenCalled();
  });

  it("AGENT_COMPLETE dispatches and sets idle", () => {
    const dispatch = vi.fn();
    const setAppState = vi.fn();
    processEvent(makeEvent("AGENT_COMPLETE"), dispatch, setAppState);
    expect(dispatch).toHaveBeenCalledWith({ type: "AGENT_COMPLETE" });
    expect(setAppState).toHaveBeenCalledWith("idle");
  });

  it("THINKING_START dispatches with timestamp", () => {
    const dispatch = vi.fn();
    const setAppState = vi.fn();
    const event = makeEvent("THINKING_START");
    processEvent(event, dispatch, setAppState);
    expect(dispatch).toHaveBeenCalledWith({
      type: "THINKING_START",
      timestamp: "2024-01-01T00:00:00Z",
    });
  });

  it("THINKING_DELTA dispatches delta", () => {
    const dispatch = vi.fn();
    const setAppState = vi.fn();
    processEvent(makeEvent("THINKING_DELTA", { delta: "hmm" }), dispatch, setAppState);
    expect(dispatch).toHaveBeenCalledWith({ type: "THINKING_DELTA", delta: "hmm" });
  });

  it("THINKING_COMPLETE dispatches content and timestamp", () => {
    const dispatch = vi.fn();
    const setAppState = vi.fn();
    const event = makeEvent("THINKING_COMPLETE", { content: "thought" });
    processEvent(event, dispatch, setAppState);
    expect(dispatch).toHaveBeenCalledWith({
      type: "THINKING_COMPLETE",
      content: "thought",
      timestamp: "2024-01-01T00:00:00Z",
    });
  });

  it("TOOL_CALL_START dispatches with mapped field names", () => {
    const dispatch = vi.fn();
    const setAppState = vi.fn();
    processEvent(
      makeEvent("TOOL_CALL_START", {
        call_id: "c1",
        tool_name: "execute_sql",
        arguments: { query: "SELECT 1" },
      }),
      dispatch,
      setAppState,
    );
    expect(dispatch).toHaveBeenCalledWith({
      type: "TOOL_CALL_START",
      callId: "c1",
      toolName: "execute_sql",
      arguments: { query: "SELECT 1" },
      timestamp: "2024-01-01T00:00:00Z",
    });
  });

  it("TOOL_CALL_COMPLETE dispatches with result", () => {
    const dispatch = vi.fn();
    const setAppState = vi.fn();
    processEvent(
      makeEvent("TOOL_CALL_COMPLETE", {
        call_id: "c1",
        tool_name: "execute_sql",
        result: { rows: [] },
      }),
      dispatch,
      setAppState,
    );
    expect(dispatch).toHaveBeenCalledWith({
      type: "TOOL_CALL_COMPLETE",
      callId: "c1",
      toolName: "execute_sql",
      result: { rows: [] },
      error: undefined,
      timestamp: "2024-01-01T00:00:00Z",
    });
  });

  it("TOOL_CALL_COMPLETE dispatches with error", () => {
    const dispatch = vi.fn();
    const setAppState = vi.fn();
    processEvent(
      makeEvent("TOOL_CALL_COMPLETE", {
        call_id: "c1",
        tool_name: "execute_sql",
        error: "bad query",
      }),
      dispatch,
      setAppState,
    );
    expect(dispatch).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "TOOL_CALL_COMPLETE",
        error: "bad query",
      }),
    );
  });

  it("TEXT_DELTA dispatches delta", () => {
    const dispatch = vi.fn();
    const setAppState = vi.fn();
    processEvent(makeEvent("TEXT_DELTA", { delta: "word" }), dispatch, setAppState);
    expect(dispatch).toHaveBeenCalledWith({ type: "TEXT_DELTA", delta: "word" });
  });

  it("TEXT_COMPLETE dispatches text", () => {
    const dispatch = vi.fn();
    const setAppState = vi.fn();
    processEvent(makeEvent("TEXT_COMPLETE", { text: "done" }), dispatch, setAppState);
    expect(dispatch).toHaveBeenCalledWith({ type: "TEXT_COMPLETE", text: "done" });
  });

  it("unknown event type is silently ignored", () => {
    const dispatch = vi.fn();
    const setAppState = vi.fn();
    processEvent(makeEvent("UNKNOWN_TYPE", {}), dispatch, setAppState);
    expect(dispatch).not.toHaveBeenCalled();
    expect(setAppState).not.toHaveBeenCalled();
  });
});
