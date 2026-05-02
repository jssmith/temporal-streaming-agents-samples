"""Terminal display for the voice analytics agent."""

import sys


def print_listening() -> None:
    print("\n--- Listening... (speak your question) ---")
    sys.stdout.flush()


def print_transcript(text: str) -> None:
    print(f"  You: {text}")
    sys.stdout.flush()


def print_thinking() -> None:
    print("  Thinking...", end="", flush=True)


def print_thinking_done() -> None:
    print(" done.")
    sys.stdout.flush()


def print_tool_call(name: str, arguments: dict, result: dict) -> None:
    if name == "execute_sql":
        query = arguments.get("query", "")
        # Truncate long queries
        if len(query) > 80:
            query = query[:77] + "..."
        row_count = result.get("row_count", "?")
        error = result.get("error")
        if error:
            print(f"  [SQL] {query}")
            print(f"        Error: {error}")
        else:
            print(f"  [SQL] {query}")
            print(f"        {row_count} rows returned")
    else:
        print(f"  [{name}] {arguments}")
    sys.stdout.flush()


def print_response(text: str) -> None:
    # Truncate very long responses for terminal readability
    if len(text) > 500:
        text = text[:497] + "..."
    print(f"  Agent: {text}")
    sys.stdout.flush()


def print_speaking() -> None:
    print("  Speaking...", end="", flush=True)


def print_speaking_done() -> None:
    print(" done.")
    sys.stdout.flush()


def print_interrupted() -> None:
    print("  [interrupted]")
    sys.stdout.flush()


def print_status(text: str) -> None:
    print(f"  [{text}]")
    sys.stdout.flush()


def print_error(message: str) -> None:
    print(f"  ERROR: {message}")
    sys.stdout.flush()


def print_banner() -> None:
    print("=" * 50)
    print("  Voice Analytics Agent (Chinook Database)")
    print("  Speak your question. Press Ctrl+C to exit.")
    print("=" * 50)
    sys.stdout.flush()
