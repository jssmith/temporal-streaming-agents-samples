import { describe, it, expect } from "vitest";
import { turnReducer, TurnState } from "../lib/chatReducer";

const emptyTurn: TurnState = { steps: [], thinkingCounter: 0 };

describe("turnReducer", () => {
  describe("THINKING lifecycle", () => {
    it("THINKING_START adds a thinking step", () => {
      const state = turnReducer(emptyTurn, {
        type: "THINKING_START",
        timestamp: "2024-01-01T00:00:00Z",
      });
      expect(state.steps).toHaveLength(1);
      expect(state.steps[0]).toEqual({
        type: "thinking",
        data: {
          id: "t0",
          status: "active",
          content: "",
          startedAt: "2024-01-01T00:00:00Z",
        },
      });
      expect(state.thinkingCounter).toBe(1);
    });

    it("THINKING_START is no-op if one already active", () => {
      const state = turnReducer(emptyTurn, { type: "THINKING_START" });
      const next = turnReducer(state, { type: "THINKING_START" });
      expect(next).toBe(state);
      expect(next.steps).toHaveLength(1);
    });

    it("THINKING_DELTA appends to active thinking step", () => {
      let state = turnReducer(emptyTurn, { type: "THINKING_START" });
      state = turnReducer(state, { type: "THINKING_DELTA", delta: "Let me " });
      state = turnReducer(state, { type: "THINKING_DELTA", delta: "think..." });
      expect(state.steps[0].type).toBe("thinking");
      if (state.steps[0].type === "thinking") {
        expect(state.steps[0].data.content).toBe("Let me think...");
      }
    });

    it("THINKING_COMPLETE marks done with duration", () => {
      let state = turnReducer(emptyTurn, {
        type: "THINKING_START",
        timestamp: "2024-01-01T00:00:00.000Z",
      });
      state = turnReducer(state, {
        type: "THINKING_COMPLETE",
        content: "I figured it out",
        timestamp: "2024-01-01T00:00:02.500Z",
      });
      expect(state.steps[0].type).toBe("thinking");
      if (state.steps[0].type === "thinking") {
        expect(state.steps[0].data.status).toBe("done");
        expect(state.steps[0].data.content).toBe("I figured it out");
        expect(state.steps[0].data.duration).toBe(2.5);
      }
    });

    it("THINKING_COMPLETE without timestamps has no duration", () => {
      let state = turnReducer(emptyTurn, { type: "THINKING_START" });
      state = turnReducer(state, {
        type: "THINKING_COMPLETE",
        content: "done",
      });
      if (state.steps[0].type === "thinking") {
        expect(state.steps[0].data.status).toBe("done");
        expect(state.steps[0].data.duration).toBeUndefined();
      }
    });
  });

  describe("TOOL_CALL lifecycle", () => {
    it("TOOL_CALL_START adds a tool call step", () => {
      const state = turnReducer(emptyTurn, {
        type: "TOOL_CALL_START",
        callId: "c1",
        toolName: "execute_sql",
        arguments: { query: "SELECT 1" },
        timestamp: "2024-01-01T00:00:00Z",
      });
      expect(state.steps).toHaveLength(1);
      expect(state.steps[0].type).toBe("tool_call");
      if (state.steps[0].type === "tool_call") {
        expect(state.steps[0].data.callId).toBe("c1");
        expect(state.steps[0].data.toolName).toBe("execute_sql");
        expect(state.steps[0].data.status).toBe("running");
      }
    });

    it("TOOL_CALL_START removes empty thinking placeholder", () => {
      let state = turnReducer(emptyTurn, { type: "THINKING_START" });
      // Thinking is active with empty content - it's a placeholder
      state = turnReducer(state, {
        type: "TOOL_CALL_START",
        callId: "c1",
        toolName: "execute_sql",
        arguments: { query: "SELECT 1" },
      });
      expect(state.steps).toHaveLength(1);
      expect(state.steps[0].type).toBe("tool_call");
    });

    it("TOOL_CALL_START keeps non-empty thinking", () => {
      let state = turnReducer(emptyTurn, { type: "THINKING_START" });
      state = turnReducer(state, { type: "THINKING_DELTA", delta: "hmm" });
      state = turnReducer(state, {
        type: "TOOL_CALL_START",
        callId: "c1",
        toolName: "bash",
        arguments: { command: "ls" },
      });
      expect(state.steps).toHaveLength(2);
      expect(state.steps[0].type).toBe("thinking");
      expect(state.steps[1].type).toBe("tool_call");
    });

    it("TOOL_CALL_COMPLETE matches callId and sets result/duration", () => {
      let state = turnReducer(emptyTurn, {
        type: "TOOL_CALL_START",
        callId: "c1",
        toolName: "execute_sql",
        arguments: { query: "SELECT 1" },
        timestamp: "2024-01-01T00:00:00.000Z",
      });
      state = turnReducer(state, {
        type: "TOOL_CALL_COMPLETE",
        callId: "c1",
        toolName: "execute_sql",
        result: { rows: [{ "1": 1 }], row_count: 1 },
        timestamp: "2024-01-01T00:00:01.200Z",
      });
      if (state.steps[0].type === "tool_call") {
        expect(state.steps[0].data.status).toBe("done");
        expect(state.steps[0].data.result).toEqual({ rows: [{ "1": 1 }], row_count: 1 });
        expect(state.steps[0].data.duration).toBeCloseTo(1.2, 1);
      }
    });

    it("TOOL_CALL_COMPLETE with error sets error status", () => {
      let state = turnReducer(emptyTurn, {
        type: "TOOL_CALL_START",
        callId: "c1",
        toolName: "execute_sql",
        arguments: { query: "BAD" },
      });
      state = turnReducer(state, {
        type: "TOOL_CALL_COMPLETE",
        callId: "c1",
        toolName: "execute_sql",
        error: "syntax error",
      });
      if (state.steps[0].type === "tool_call") {
        expect(state.steps[0].data.status).toBe("error");
        expect(state.steps[0].data.error).toBe("syntax error");
      }
    });
  });

  describe("TEXT lifecycle", () => {
    it("TEXT_DELTA creates output step if none exists", () => {
      const state = turnReducer(emptyTurn, {
        type: "TEXT_DELTA",
        delta: "Hello",
      });
      expect(state.steps).toHaveLength(1);
      expect(state.steps[0]).toEqual({ type: "output", text: "Hello" });
    });

    it("TEXT_DELTA appends to existing output step", () => {
      let state = turnReducer(emptyTurn, { type: "TEXT_DELTA", delta: "Hel" });
      state = turnReducer(state, { type: "TEXT_DELTA", delta: "lo" });
      expect(state.steps).toHaveLength(1);
      expect(state.steps[0]).toEqual({ type: "output", text: "Hello" });
    });

    it("TEXT_DELTA removes empty thinking placeholder", () => {
      let state = turnReducer(emptyTurn, { type: "THINKING_START" });
      state = turnReducer(state, { type: "TEXT_DELTA", delta: "answer" });
      expect(state.steps).toHaveLength(1);
      expect(state.steps[0].type).toBe("output");
    });

    it("TEXT_COMPLETE sets final text on existing output", () => {
      let state = turnReducer(emptyTurn, { type: "TEXT_DELTA", delta: "partial" });
      state = turnReducer(state, { type: "TEXT_COMPLETE", text: "full answer" });
      expect(state.steps).toHaveLength(1);
      expect(state.steps[0]).toEqual({ type: "output", text: "full answer" });
    });

    it("TEXT_COMPLETE creates output step if none exists", () => {
      const state = turnReducer(emptyTurn, {
        type: "TEXT_COMPLETE",
        text: "done",
      });
      expect(state.steps).toHaveLength(1);
      expect(state.steps[0]).toEqual({ type: "output", text: "done" });
    });
  });
});
