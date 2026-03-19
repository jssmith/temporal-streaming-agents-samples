import { Step, ToolCallData } from "../app/components/ChatMessage";

// --- Types ---

export interface SSEEvent {
  type: string;
  timestamp: string;
  data: Record<string, unknown>;
}

export interface ChatMessage {
  role: "user" | "agent";
  content?: string;
  steps?: Step[];
}

export interface TurnState {
  steps: Step[];
  thinkingCounter: number;
}

export interface ChatState {
  messages: ChatMessage[];
  currentTurn: TurnState;
}

export type ChatAction =
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

export const initialChatState: ChatState = {
  messages: [],
  currentTurn: { steps: [], thinkingCounter: 0 },
};

// --- Reducers ---

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
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

export function turnReducer(state: TurnState, action: ChatAction): TurnState {
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
