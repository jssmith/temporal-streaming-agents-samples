"""Audio recording and playback for the voice terminal.

Uses sounddevice for mic input (16kHz for Whisper) and speaker output (24kHz for TTS).
Energy-based VAD detects silence to end recording.
"""

import asyncio
import io
import logging
import threading
import wave

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

INPUT_SAMPLE_RATE = 16000  # Whisper expects 16kHz
OUTPUT_SAMPLE_RATE = 24000  # OpenAI TTS outputs 24kHz
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit


def print_audio_devices() -> None:
    """Print the default input and output audio devices."""
    try:
        input_dev = sd.query_devices(kind="input")
        output_dev = sd.query_devices(kind="output")
        print(f"  Microphone: {input_dev['name']}")
        print(f"  Speaker:    {output_dev['name']}")
    except Exception as e:
        logger.warning("Could not query audio devices: %s", e)


async def record_until_silence(
    sample_rate: int = INPUT_SAMPLE_RATE,
    silence_threshold: float = 0.02,
    silence_duration: float = 1.5,
    max_duration: float = 30.0,
    pre_speech_buffer: float = 0.3,
) -> bytes:
    """Record from microphone until silence is detected after speech.

    Returns WAV bytes suitable for the Whisper API.
    """
    loop = asyncio.get_running_loop()
    done = asyncio.Event()

    frames: list[np.ndarray] = []
    speech_started = False
    silent_frames = 0
    frames_for_silence = int(silence_duration * sample_rate / 1024)
    max_frames = int(max_duration * sample_rate / 1024)
    pre_speech_frames = int(pre_speech_buffer * sample_rate / 1024)
    pre_buffer: list[np.ndarray] = []

    def callback(indata: np.ndarray, frame_count: int, time_info, status):
        nonlocal speech_started, silent_frames
        if status:
            logger.warning("Input status: %s", status)

        chunk = indata[:, 0].copy()
        rms = np.sqrt(np.mean(chunk ** 2))

        if not speech_started:
            pre_buffer.append(chunk)
            if len(pre_buffer) > pre_speech_frames:
                pre_buffer.pop(0)
            if rms > silence_threshold:
                speech_started = True
                frames.extend(pre_buffer)
                frames.append(chunk)
                silent_frames = 0
        else:
            frames.append(chunk)
            if rms < silence_threshold:
                silent_frames += 1
                if silent_frames >= frames_for_silence:
                    loop.call_soon_threadsafe(done.set)
            else:
                silent_frames = 0

            if len(frames) >= max_frames:
                loop.call_soon_threadsafe(done.set)

    stream = sd.InputStream(
        samplerate=sample_rate,
        channels=CHANNELS,
        dtype=np.float32,
        blocksize=1024,
        callback=callback,
    )

    with stream:
        await done.wait()

    if not frames:
        return b""

    audio = np.concatenate(frames)
    audio_int16 = (audio * 32767).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())

    return buf.getvalue()


class AudioPlayer:
    """Plays PCM audio chunks with interruption support.

    Also monitors mic input for speech detection (barge-in).
    Mutes the mic while audio is actively playing to avoid feedback.
    """

    def __init__(self, sample_rate: int = OUTPUT_SAMPLE_RATE):
        self._sample_rate = sample_rate
        self._buffer = b""
        self._lock = threading.Lock()
        self._playing = False
        self._outputting = False  # True when output callback is producing audio
        self._interrupted = False
        self._ever_enqueued = False  # Track whether any audio was ever enqueued
        self._stream: sd.OutputStream | None = None
        # Speech detection for interruption
        self._speech_detected = False
        self._input_stream: sd.InputStream | None = None
        self._speech_threshold = 0.04  # Higher threshold to avoid feedback

    def start(self) -> None:
        """Start the output stream."""
        self._playing = True
        self._interrupted = False
        self._speech_detected = False
        self._ever_enqueued = False
        self._outputting = False
        self._buffer = b""

        def output_callback(outdata: np.ndarray, frames: int, time_info, status):
            if status:
                logger.warning("Output status: %s", status)
            bytes_needed = frames * CHANNELS * SAMPLE_WIDTH
            with self._lock:
                if len(self._buffer) >= bytes_needed:
                    chunk = self._buffer[:bytes_needed]
                    self._buffer = self._buffer[bytes_needed:]
                    audio_int16 = np.frombuffer(chunk, dtype=np.int16)
                    outdata[:, 0] = audio_int16.astype(np.float32) / 32767.0
                    self._outputting = True
                else:
                    outdata.fill(0)
                    self._outputting = False

        self._stream = sd.OutputStream(
            samplerate=self._sample_rate,
            channels=CHANNELS,
            dtype=np.float32,
            blocksize=1024,
            callback=output_callback,
        )
        self._stream.start()

    def start_speech_detection(self, threshold: float = 0.06) -> None:
        """Start monitoring mic for speech (used during playback for interruption).

        Uses a higher threshold and requires several consecutive loud frames
        to distinguish real speech from speaker feedback.
        """
        self._speech_threshold = threshold
        self._speech_detected = False
        consecutive_loud = 0
        # Require ~150ms of sustained loud input (not just a brief feedback spike)
        frames_needed = 3

        def input_callback(indata: np.ndarray, frames: int, time_info, status):
            nonlocal consecutive_loud
            # Suppress detection while the speaker is actively outputting
            # to avoid picking up our own TTS audio as speech.
            if self._outputting:
                consecutive_loud = 0
                return
            rms = np.sqrt(np.mean(indata[:, 0] ** 2))
            if rms > self._speech_threshold:
                consecutive_loud += 1
                if consecutive_loud >= frames_needed:
                    self._speech_detected = True
            else:
                consecutive_loud = 0

        self._input_stream = sd.InputStream(
            samplerate=INPUT_SAMPLE_RATE,
            channels=CHANNELS,
            dtype=np.float32,
            blocksize=1024,
            callback=input_callback,
        )
        self._input_stream.start()

    def stop_speech_detection(self) -> None:
        if self._input_stream:
            self._input_stream.stop()
            self._input_stream.close()
            self._input_stream = None

    @property
    def speech_detected(self) -> bool:
        return self._speech_detected

    def enqueue(self, pcm_data: bytes) -> None:
        """Add PCM audio data to the playback buffer."""
        with self._lock:
            self._buffer += pcm_data
            self._ever_enqueued = True

    def interrupt(self) -> None:
        """Immediately clear the playback buffer."""
        with self._lock:
            self._buffer = b""
        self._interrupted = True

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return len(self._buffer) > 0

    async def wait_until_done(self) -> None:
        """Wait until the playback buffer is drained or interrupted.

        Won't return prematurely if audio hasn't been enqueued yet.
        """
        # Wait for audio to start being enqueued
        while not self._ever_enqueued and not self._interrupted:
            await asyncio.sleep(0.05)

        # Wait for buffer to drain
        while True:
            with self._lock:
                if self._interrupted:
                    break
                if len(self._buffer) == 0 and self._ever_enqueued:
                    # Buffer drained — wait a tiny bit more for the output
                    # callback to finish playing the last chunk
                    break
            if self._speech_detected:
                self.interrupt()
                break
            await asyncio.sleep(0.05)

        # Small grace period for the last audio chunk to finish playing
        if not self._interrupted:
            await asyncio.sleep(0.2)

    def stop(self) -> None:
        """Stop the output stream."""
        self._playing = False
        self.stop_speech_detection()
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
