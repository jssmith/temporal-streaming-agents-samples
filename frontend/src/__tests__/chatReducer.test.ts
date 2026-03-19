import { describe, it, expect } from "vitest";
import { chatReducer, initialChatState, ChatState } from "../lib/chatReducer";

describe("chatReducer", () => {
  it("USER_MESSAGE adds a user message", () => {
    const state = chatReducer(initialChatState, {
      type: "USER_MESSAGE",
      content: "hello",
    });
    expect(state.messages).toEqual([{ role: "user", content: "hello" }]);
  });

  it("USER_MESSAGE deduplicates consecutive identical messages", () => {
    const state: ChatState = {
      ...initialChatState,
      messages: [{ role: "user", content: "hello" }],
    };
    const next = chatReducer(state, { type: "USER_MESSAGE", content: "hello" });
    expect(next).toBe(state); // same reference = no change
  });

  it("USER_MESSAGE does not dedup different content", () => {
    const state: ChatState = {
      ...initialChatState,
      messages: [{ role: "user", content: "hello" }],
    };
    const next = chatReducer(state, { type: "USER_MESSAGE", content: "world" });
    expect(next.messages).toHaveLength(2);
    expect(next.messages[1]).toEqual({ role: "user", content: "world" });
  });

  it("AGENT_COMPLETE snapshots turn into message and resets turn", () => {
    const state: ChatState = {
      messages: [{ role: "user", content: "hi" }],
      currentTurn: {
        steps: [{ type: "output", text: "Hello!" }],
        thinkingCounter: 1,
      },
    };
    const next = chatReducer(state, { type: "AGENT_COMPLETE" });
    expect(next.messages).toHaveLength(2);
    expect(next.messages[1]).toEqual({
      role: "agent",
      steps: [{ type: "output", text: "Hello!" }],
    });
    expect(next.currentTurn).toEqual({ steps: [], thinkingCounter: 0 });
  });

  it("AGENT_COMPLETE with empty turn is no-op", () => {
    const state = chatReducer(initialChatState, { type: "AGENT_COMPLETE" });
    expect(state).toBe(initialChatState);
  });

  it("CLEAR resets everything", () => {
    const state: ChatState = {
      messages: [{ role: "user", content: "hi" }],
      currentTurn: {
        steps: [{ type: "output", text: "text" }],
        thinkingCounter: 3,
      },
    };
    const next = chatReducer(state, { type: "CLEAR" });
    expect(next.messages).toEqual([]);
    expect(next.currentTurn).toEqual({ steps: [], thinkingCounter: 0 });
  });

  it("turn actions delegate to turnReducer", () => {
    const state = chatReducer(initialChatState, { type: "THINKING_START" });
    expect(state.currentTurn.steps).toHaveLength(1);
    expect(state.currentTurn.steps[0].type).toBe("thinking");
  });
});
