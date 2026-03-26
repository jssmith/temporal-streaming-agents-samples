# Record Demo

Record a video walkthrough of a demo application using Playwright.

## Usage

```
/record-demo [app]
```

Where `app` is one of:
- `analytics` (default) — the analytics agent in `temporal-streaming-agents-samples/`

If no app is specified, defaults to `analytics`.

## Instructions

When this skill is invoked:

1. **Verify prerequisites**:
   - `OPENAI_API_KEY` must be set
   - The backend and frontend should be running, OR Playwright's webServer config will start them

2. **Run the recording**:
   ```bash
   cd temporal-streaming-agents-samples
   npm run record:demo
   ```

3. **Find the output**: Videos are saved to `temporal-streaming-agents-samples/test-results/recording/`. Each test produces a `.webm` file.

4. **Report results**:
   - Whether the recording completed successfully
   - Path to the output video file(s)
   - Duration of the recording
   - Any errors or issues encountered

## How It Works

The recording uses a dedicated Playwright config (`playwright.recording.config.ts`) that:
- Enables video capture at 1280x720
- Runs tests from `tests/recording/` (separate from the E2E test suite)
- Has no retries (we want one clean run, not test recovery)
- Uses generous timeouts to accommodate agent response times

The demo script (`tests/recording/demo.spec.ts`) walks through:
1. Clicking a suggested prompt (SQL tool call + streaming table)
2. Multi-turn follow-up query
3. Creating a new session with a different query type
4. Switching back to the first session
5. Starting a query and interrupting with Escape

It uses slow typing and pauses between steps to make the video watchable.

## Customization

To change what the recording covers, edit `tests/recording/demo.spec.ts`.

Key timing constants at the top of the file:
- `TYPING_DELAY` — ms per keystroke (default: 60)
- `PAUSE_AFTER_COMPLETE` — pause after agent finishes (default: 3000ms)
- `PAUSE_BEFORE_ACTION` — pause before next action (default: 1000ms)

## Converting Video

Playwright outputs `.webm` files. To convert to `.mp4`:

```bash
ffmpeg -i input.webm -c:v libx264 -crf 23 output.mp4
```
