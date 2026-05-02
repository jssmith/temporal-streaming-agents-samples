# Voice Analytics Agent: Architecture

Implementation notes for the Temporal-backed voice agent at
`apps/voice-terminal/`. For the general streaming architecture, see the
[top-level README](../../README.md).

## Project Structure

```
apps/voice-terminal/
└── src/
    ├── workflows.py       # Temporal workflow (voice agent loop + state)
    ├── activities.py      # transcribe, model_call (streams TTS), execute_sql
    ├── main_temporal.py   # Half-duplex client (record → signal → subscribe → play)
    ├── main_simple.py     # Non-Temporal version for comparison
    ├── audio.py           # Recording (VAD) + playback
    ├── tts.py / transcribe.py  # OpenAI TTS / Whisper wrappers
    ├── display.py         # Terminal output helpers
    ├── types.py           # Pydantic models (workflow contract)
    ├── worker.py          # Temporal worker entry point
    └── agent.py           # Non-Temporal agent loop (used by main_simple)
```

Shared library: `packages/shared/` (`analytics_shared.database`,
`analytics_shared.sql_tool`, `analytics_shared.tts_chunking`,
`analytics_shared.constants`, `analytics_shared.types`).

## Workflow Contract

| Primitive | Name | Purpose |
|---|---|---|
| Signal | `start_turn` | Client sends recorded audio (base64 WAV) for a turn |
| Signal | `truncate` | Client acks a played-through stream offset; workflow drops `[base, offset)` from the live stream |
| Signal | `close_session` | Drain and exit the workflow |
| Query | `get_state` | Conversation messages and `turn_active` flag |

There is no `interrupt` signal — mid-playback barge-in is not implemented
(see *Deferred work* below).

## Streaming Transport

Two topics share a single `WorkflowStream`:

```python
self.stream  = WorkflowStream(prior_state=state.stream_state)
self.events  = self.stream.topic(EVENTS_TOPIC, type=dict)
# AUDIO_TOPIC is published from the activity; the workflow doesn't keep a handle
```

- **Workflow → topic**: lifecycle events (`STATUS`, `TRANSCRIPT`, `TOOL_CALL`,
  `RESPONSE_TEXT`, `TURN_COMPLETE`) are published from the workflow with
  `self.events.publish({...})`.
- **Activity → topic**: `model_call` opens a
  `WorkflowStreamClient.from_within_activity(batch_interval=0.1s)`, publishes
  TTS audio chunks to `AUDIO_TOPIC` (one base64 PCM blob per sentence, with
  `force_flush=True` so playback isn't delayed). The 0.1s batch interval is
  override-able via `WORKFLOW_STREAM_BATCH_INTERVAL`.
- **Client → topic**: `main_temporal.py` calls `stream.subscribe(topics=[
  AUDIO_TOPIC, EVENTS_TOPIC], from_offset=last_offset)`. Items arrive
  interleaved by global offset; the client dispatches by `item.topic`.

## Truncation and Continue-as-New

These are **independent** mechanisms.

**Truncation** is a continuous "the client played up to offset N, you can
drop it" ack. The workflow's `truncate(N)` signal calls
`self.stream.truncate(N)`, which removes `[base_offset, N)` from the live
in-memory log. The publish events that originally wrote those items are
still in the immutable workflow event history.

**Continue-as-new** is what actually shrinks the persistent footprint. When
`workflow.info().is_continue_as_new_suggested()` returns true (Temporal
decides this based on history size and event count), the workflow snapshots
its current `_log` (already-truncated state) into a new run via
`self.stream.continue_as_new(...)`. The new run's history starts fresh and
inherits only what hadn't been truncated.

**Per-turn ack policy.** The client signals `truncate(last_offset)` once
per turn, after `TURN_COMPLETE` is received and `AudioPlayer.wait_until_done`
confirms the audio actually played. This is intentionally coarse — there's
no correctness reason to be finer-grained, and per-turn keeps the signal
traffic low. With `max_output_tokens` bounding each turn's audio, at most
one turn's worth of un-acked audio (~10 MB upper bound on the wire) is
ever in the live stream.

**CAN waits for truncation to catch up.** A turn's audio sits un-acked
in the live stream until the client finishes playback and signals
`truncate`. The CAN trigger therefore waits for `len(stream._log) == 0`
before snapshotting — otherwise the un-acked audio rides into the new
run's args and risks exceeding Temporal's per-payload limit (~4 MB,
which is below the per-turn cap of ~11 MB at 700 tokens). Signals that
land in the handoff window (`close_session`, `start_turn`) are
preserved by carrying `closed` and `pending_audio` in
`VoiceWorkflowState`, so neither shutdown requests nor in-flight turns
are dropped across CAN.

## Per-turn Bandwidth Bound

`max_output_tokens=700` on the OpenAI Responses API call gives a hard
ceiling on the audio volume per turn:

- TTS-1 PCM: 24 kHz × 1 ch × 16-bit = 48 KB/s raw
- Base64'd in the `{"audio_base64": ...}` publish envelope: ~64 KB/s
- ~150 wpm × ~5 chars/word ≈ 15 chars/s of speech
- 700 tokens ≈ 2700 chars ≈ 180 s of speech ≈ 11.5 MB base64'd

Comfortably under Temporal's per-payload soft warning (512 KB *per signal
flush*; the 0.1 s batch interval keeps individual signals small) and the
hard limit (~4 MB).

## Failure Modes

| Failure | Impact | Recovery |
|---|---|---|
| **Client disconnects mid-turn** | Subscription drops; audio keeps publishing into the workflow stream | Client reconnects with `from_offset=last_offset` and resumes. Un-acked audio is still there to be played. |
| **Worker crash** | Activity and workflow tasks stop | Temporal reassigns. Workflow state, including the stream log, is rebuilt from event history. |
| **Activity retry mid-stream** | Partial audio from the failed attempt is in the stream | Items remain; on retry, the activity publishes from the start again. The client hears overlapping audio (deduplication is not implemented). |
| **Temporal payload limit** | Hit if a turn produces too much audio | Bounded above by `max_output_tokens=700`. If raised, see *Backpressure* below. |

## Deferred Work

These are intentionally not implemented. Listed here so the next contributor
knows the shape of each before attacking it.

### Mid-playback barge-in / interrupt

Removed entirely (commit `174ef55`) because the previous implementation
was half-wired (workflow had the signal, client never sent it) and the
single-mic-channel design read the speaker's own output back as input.
A real implementation would need duplex audio with feedback suppression
or an out-of-band push-to-talk button. Until then the demo is half-duplex
by design: speak, then listen.

### Backpressure for sustained TTS > playback

If TTS sustains a higher byte rate than playback for long enough, audio
accumulates in the un-truncated stream and `is_continue_as_new_suggested`
will fire (or worse, a single signal will exceed the per-payload limit).
The current per-turn `max_output_tokens=700` cap dodges this for typical
inputs but doesn't generalize. Two designs to choose from when this
becomes a real problem:

1. **External payload codec.** Implement a `temporalio.converter.PayloadCodec`
   that off-ramps payloads above a threshold (e.g., 32 KB) to S3 / local FS /
   Redis and replaces them with a reference. Workflow history holds only
   pointers; the codec rehydrates on read. Pro: no application-level changes.
   Con: external dependency, garbage-collection design (when can refs be
   freed after CAN?).

2. **Activity-side wait-for-drain.** Add a workflow `update` like
   `wait_for_drain(target_offset)` that blocks on
   `wait_condition(lambda: self.stream.base_offset >= target_offset)`.
   The activity calls this update when its un-acked output exceeds a
   threshold; TTS generation pauses until the client catches up. Pro:
   pure-Temporal, matches the project's value prop. Con: more code, and
   the activity needs heartbeats while waiting.

Either is a real design; pick based on operational preference. Option 2 is
slightly more aligned with the project's "show off Temporal patterns" vibe.

### Per-sentence hard cap

If a single sentence is unusually long (no punctuation for a thousand
characters), the resulting TTS blob can approach the per-payload soft
warning even with the per-turn cap above. The fix is to split a sentence
at the last whitespace before some byte/char ceiling before publishing.
Today the per-turn cap subsumes this concern; revisit if the model starts
producing punctuation-free runs.

### TTS pipelining

`await _generate_tts(sentence)` runs inline inside the model-stream
`async for` loop. While TTS generates (~1 s/sentence), the activity is not
reading new model deltas. The model itself buffers, so this isn't lost
work — but the activity's wall-clock time is sequential, and the user-
perceived latency for the *first* audio is fine but later sentences arrive
slower than they could.

A pipelined version would `asyncio.create_task(_generate_tts(...))` and
funnel completed TTS through an ordered queue to a publisher task.
~30 lines of plumbing; defer until latency complaints surface.

### Audio-format efficiency

Audio is currently published as `{"audio_base64": <str>}`, which is a
JSON dict carrying a base64-encoded PCM blob. Base64 inflates by 33% and
the JSON wrapping adds another small constant. Publishing as raw `bytes`
through a `type=bytes` topic would skip both layers. ~Modest payload
reduction, no behavior change. Worth doing alongside any payload-related
work.

### Test coverage gaps

- All Temporal-backed tests connect to `localhost:7233` via `Client.connect`.
  A first-time contributor who runs `pytest` without a dev server gets
  opaque connection errors. Switching to `WorkflowEnvironment.start_local()`
  in the fixtures would fix that.
- No test exercises the truncation + CAN combo end-to-end. The existing
  `VoiceWorkflowForceCAN` test forces CAN explicitly but doesn't drive the
  client's per-turn truncation signal. Worth adding.
