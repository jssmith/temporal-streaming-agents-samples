"""Sentence-boundary detection for streaming text-to-speech.

Activities buffer streamed model text and flush at sentence boundaries so
TTS can begin while later tokens are still arriving. `MIN_FLUSH_LEN` keeps
short fragments from being spoken as their own utterance.
"""

import re

SENTENCE_END = re.compile(r"(?<=[.!?:])(?:\s|$)")
MIN_FLUSH_LEN = 30
