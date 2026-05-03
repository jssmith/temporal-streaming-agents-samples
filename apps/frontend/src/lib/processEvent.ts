import { SSEEvent, ChatAction } from "./chatReducer";

export type AppState = "idle" | "sending" | "running" | "error";

/**
 * Maps an SSE event to reducer dispatch calls and app state changes.
 * Pure mapping function -- no side effects beyond the callbacks.
 */
export function processEvent(
  event: SSEEvent,
  dispatch: (action: ChatAction) => void,
  setAppState: (state: AppState) => void,
): void {
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
