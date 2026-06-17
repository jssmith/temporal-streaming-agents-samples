import { vscDarkPlus } from "react-syntax-highlighter/dist/esm/styles/prism";

// Shared react-syntax-highlighter theme + container style used by every
// code block (inline tool execution and streamed markdown fences) so the
// look stays identical across the app.
export const codeHighlighterStyle = vscDarkPlus;

export const codeHighlighterCustomStyle = {
  margin: 0,
  padding: "0.75rem",
  fontSize: "0.8125rem",
  background: "#1e1e3a",
  borderRadius: "0.375rem",
} as const;
