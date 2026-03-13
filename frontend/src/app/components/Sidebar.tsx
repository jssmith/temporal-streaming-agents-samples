"use client";

interface SessionTab {
  sessionId: string;
  preview: string;
  messageCount: number;
}

interface SidebarProps {
  sessions: SessionTab[];
  activeSessionId: string | null;
  onSelectSession: (sessionId: string) => void;
  onNewSession: () => void;
  onDeleteSession: (sessionId: string) => void;
}

export default function Sidebar({
  sessions,
  activeSessionId,
  onSelectSession,
  onNewSession,
  onDeleteSession,
}: SidebarProps) {
  return (
    <aside className="w-64 shrink-0 bg-[#16162a] border-r border-border flex flex-col h-screen">
      <div className="p-3">
        <button
          onClick={onNewSession}
          className="w-full flex items-center gap-2 px-3 py-2 text-sm text-gray-300 border border-border rounded-lg hover:bg-surface transition-colors"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M12 5v14M5 12h14" />
          </svg>
          New chat
        </button>
      </div>

      <nav className="flex-1 overflow-y-auto px-2 pb-3">
        {sessions.map((session) => {
          const isActive = session.sessionId === activeSessionId;
          return (
            <div
              key={session.sessionId}
              className={`group flex items-center rounded-lg mb-0.5 transition-colors ${
                isActive
                  ? "bg-surface text-gray-200"
                  : "text-gray-400 hover:bg-surface/50 hover:text-gray-300"
              }`}
            >
              <button
                onClick={() => onSelectSession(session.sessionId)}
                className="flex-1 text-left px-3 py-2 text-sm truncate min-w-0"
              >
                {session.preview}
              </button>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onDeleteSession(session.sessionId);
                }}
                className="shrink-0 px-2 py-2 text-gray-500 hover:text-red-400 opacity-0 group-hover:opacity-100 transition-opacity"
                title="Delete session"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M18 6L6 18M6 6l12 12" />
                </svg>
              </button>
            </div>
          );
        })}
      </nav>
    </aside>
  );
}

export type { SessionTab };
