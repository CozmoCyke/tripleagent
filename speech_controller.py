"""Standalone speech playback controller for progressive balloon text and mouth cues."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, List, Optional

_MOUTH_CLOSED = "mouth_closed"

_OPEN_MOUTHS = [
    "mouth_narrow",
    "mouth_medium",
    "mouth_wide_open_1",
    "mouth_wide_open_2",
    "mouth_wide_open_3",
    "mouth_wide_open_4",
]


@dataclass(frozen=True)
class MouthCue:
    """Normalized mouth cue.

    The controller keeps this small on purpose: the caller only needs the current
    mouth name, but the timing fields make the object useful for later pipeline
    steps without forcing a rewrite here.
    """

    mouth: str
    start: float
    end: float
    source: str = "lipsync"
    payload: dict[str, Any] = field(default_factory=dict)


class SpeechController:
    """Pure-Python speech controller.

    This object does not know anything about Tk, ACS, or rendering internals.
    It simply advances a typewriter-style text reveal and a mouth timeline.
    """

    def __init__(
        self,
        text: str,
        lipsync,
        chars_per_second: float = 18.0,
        comma_pause: float = 0.12,
        sentence_pause: float = 0.30,
        reveal_mode: str = "char",
    ):
        self.text = "" if text is None else str(text)
        self.lipsync = lipsync
        self.chars_per_second = max(1.0, float(chars_per_second))
        self.comma_pause = max(0.0, float(comma_pause))
        self.sentence_pause = max(0.0, float(sentence_pause))
        self.reveal_mode = str(reveal_mode or "char").strip().lower()

        self._started = False
        self._done = False
        self._elapsed = 0.0
        self._visible_count = 0
        self._visible_text = ""
        self._current_mouth = _MOUTH_CLOSED
        self._mouth_cues: List[MouthCue] = []
        self._reveal_times: List[float] = []
        self._speech_duration = 0.0

    def start(self) -> None:
        """Initialize schedules and reset playback state."""
        self._elapsed = 0.0
        self._visible_count = 0
        self._visible_text = ""
        self._done = False
        self._started = True
        self._mouth_cues = self._normalize_mouth_cues(self._resolve_lipsync_cues(self.text))
        self._reveal_times = self._build_reveal_times(self.text)
        self._speech_duration = self._estimate_total_duration()
        self._current_mouth = self._mouth_for_time(0.0)

    def update(self, dt: float) -> None:
        """Advance the controller by dt seconds."""
        if not self._started:
            self.start()
        if self._done:
            return

        self._elapsed = max(0.0, self._elapsed + max(0.0, float(dt)))
        self._visible_count = self._count_visible_chars(self._elapsed)
        self._visible_text = self.text[: self._visible_count]
        self._current_mouth = self._mouth_for_time(self._elapsed)

        if self._elapsed >= self._speech_duration:
            self._visible_count = len(self.text)
            self._visible_text = self.text
            self._current_mouth = _MOUTH_CLOSED
            self._done = True

    @property
    def visible_text(self) -> str:
        if not self._started:
            return ""
        return self._visible_text

    @property
    def current_mouth(self):
        if not self._started:
            return None
        return self._current_mouth

    @property
    def is_done(self) -> bool:
        return self._done

    def _resolve_lipsync_cues(self, text: str) -> Any:
        """Call the provided lipsync source in a tolerant way.

        The historical prototype is not guaranteed to be present in every clone,
        so we accept either a callable, a module, or an object with a small set of
        conventional method names.
        """
        source = self.lipsync
        if source is None:
            return None

        if callable(source):
            return source(text)

        for attr_name in (
            "build_mouth_cues",
            "generate_mouth_cues",
            "make_mouth_cues",
            "lipsync_text",
            "lipsync",
            "generate",
            "analyze",
        ):
            candidate = getattr(source, attr_name, None)
            if callable(candidate):
                try:
                    return candidate(text)
                except TypeError:
                    continue

        return None

    def _normalize_mouth_cues(self, raw: Any) -> List[MouthCue]:
        if raw is None:
            return self._fallback_mouth_cues()

        if isinstance(raw, dict):
            for key in ("mouth_cues", "cues", "visemes", "timeline", "mouth_timeline", "segments"):
                if key in raw:
                    return self._normalize_mouth_cues(raw[key])
            return self._fallback_mouth_cues()

        if isinstance(raw, MouthCue):
            return [raw]

        if isinstance(raw, (str, bytes)):
            return self._fallback_mouth_cues()

        if not isinstance(raw, Sequence):
            raw = list(raw) if isinstance(raw, Iterable) else []

        cues: List[MouthCue] = []
        for item in raw:
            cue = self._coerce_mouth_cue(item)
            if cue is not None:
                cues.append(cue)

        if not cues:
            return self._fallback_mouth_cues()

        # If the source did not provide usable timing, spread cues across the
        # estimated speech duration so the controller still behaves sensibly.
        if all(cue.start == 0.0 and cue.end == 0.0 for cue in cues):
            total = max(self._estimate_text_duration(), 0.1)
            step = total / max(1, len(cues))
            timed: List[MouthCue] = []
            current = 0.0
            for cue in cues:
                end = current + step
                timed.append(MouthCue(cue.mouth, current, end, cue.source, cue.payload))
                current = end
            cues = timed
        else:
            cues = self._sort_and_fill_cues(cues)

        return cues

    def _coerce_mouth_cue(self, item: Any) -> Optional[MouthCue]:
        if item is None:
            return None
        if isinstance(item, MouthCue):
            return item
        if isinstance(item, str):
            return MouthCue(mouth=self._normalize_mouth_name(item), start=0.0, end=0.0, source="string")
        if isinstance(item, dict):
            mouth = item.get("mouth") or item.get("name") or item.get("viseme") or item.get("shape")
            if mouth is None:
                return None
            start = item.get("start", item.get("time", item.get("at", 0.0)))
            end = item.get("end", None)
            duration = item.get("duration", None)
            start_f = max(0.0, float(start or 0.0))
            if end is None and duration is not None:
                end_f = start_f + max(0.0, float(duration or 0.0))
            elif end is None:
                end_f = start_f
            else:
                end_f = max(start_f, float(end))
            return MouthCue(
                mouth=self._normalize_mouth_name(str(mouth)),
                start=start_f,
                end=end_f,
                source=str(item.get("source", "dict")),
                payload=dict(item),
            )

        mouth = getattr(item, "mouth", None) or getattr(item, "name", None) or getattr(item, "viseme", None)
        if mouth is None:
            return None

        start = getattr(item, "start", getattr(item, "time", getattr(item, "at", 0.0)))
        end = getattr(item, "end", None)
        duration = getattr(item, "duration", None)
        start_f = max(0.0, float(start or 0.0))
        if end is None and duration is not None:
            end_f = start_f + max(0.0, float(duration or 0.0))
        elif end is None:
            end_f = start_f
        else:
            end_f = max(start_f, float(end))
        payload = {}
        if hasattr(item, "__dict__"):
            payload = {k: v for k, v in vars(item).items() if not k.startswith("_")}
        return MouthCue(
            mouth=self._normalize_mouth_name(str(mouth)),
            start=start_f,
            end=end_f,
            source=type(item).__name__,
            payload=payload,
        )

    def _sort_and_fill_cues(self, cues: List[MouthCue]) -> List[MouthCue]:
        ordered = sorted(cues, key=lambda cue: (cue.start, cue.end, cue.mouth))
        filled: List[MouthCue] = []
        last_end = 0.0
        for cue in ordered:
            start = max(last_end, cue.start)
            end = max(start, cue.end)
            filled.append(MouthCue(cue.mouth, start, end, cue.source, cue.payload))
            last_end = end
        return filled

    def _fallback_mouth_cues(self) -> List[MouthCue]:
        text = self.text or ""
        if not text:
            return [MouthCue(_MOUTH_CLOSED, 0.0, 0.1, "fallback")]

        chars = list(text)
        duration = self._estimate_text_duration()
        step = duration / max(1, len(chars))

        cues: List[MouthCue] = []
        current = 0.0
        for ch in chars:
            mouth = self._mouth_for_character(ch)
            end = current + step
            if ch in ",;":
                end += self.comma_pause
            elif ch in ".!?":
                end += self.sentence_pause
            cues.append(MouthCue(mouth=mouth, start=current, end=end, source="fallback", payload={"char": ch}))
            current = end

        return cues

    def _build_reveal_times(self, text: str) -> List[float]:
        if not text:
            return []

        if self.reveal_mode == "word":
            return self._build_word_reveal_times(text)

        cps = self.chars_per_second
        interval = 1.0 / cps
        times: List[float] = []
        current = 0.0
        for ch in text:
            current += interval
            if ch in ",;":
                current += self.comma_pause
            elif ch in ".!?":
                current += self.sentence_pause
            times.append(current)
        return times

    def _build_word_reveal_times(self, text: str) -> List[float]:
        cps = self.chars_per_second
        interval = 1.0 / cps
        times: List[float] = []
        current = 0.0
        word_count = 0
        for ch in text:
            current += interval
            if ch.isspace():
                word_count += 1
                current += 0.05
            elif ch in ",;":
                current += self.comma_pause
            elif ch in ".!?":
                current += self.sentence_pause
            times.append(current if word_count else current)
        return times

    def _count_visible_chars(self, elapsed: float) -> int:
        if not self._reveal_times:
            return len(self.text) if elapsed > 0 else 0
        count = 0
        for reveal_time in self._reveal_times:
            if elapsed >= reveal_time:
                count += 1
            else:
                break
        return min(count, len(self.text))

    def _mouth_for_time(self, elapsed: float) -> str:
        if not self._mouth_cues:
            return _MOUTH_CLOSED
        for cue in self._mouth_cues:
            if cue.start <= elapsed <= cue.end:
                return cue.mouth
        if elapsed > self._mouth_cues[-1].end:
            return _MOUTH_CLOSED
        return self._mouth_cues[0].mouth

    def _estimate_text_duration(self) -> float:
        words = max(1, len(self.text.split()))
        chars = len(self.text)
        base = chars / self.chars_per_second
        punctuation = sum(1 for ch in self.text if ch in ",;") * self.comma_pause
        punctuation += sum(1 for ch in self.text if ch in ".!?") * self.sentence_pause
        return max(0.1, base + punctuation + 0.08 * words)

    def _estimate_total_duration(self) -> float:
        reveal_duration = self._reveal_times[-1] if self._reveal_times else self._estimate_text_duration()
        if self._mouth_cues:
            mouth_end = max(cue.end for cue in self._mouth_cues)
        else:
            mouth_end = reveal_duration
        return max(reveal_duration, mouth_end) + 0.15

    def _normalize_mouth_name(self, name: str) -> str:
        cleaned = str(name or "").strip()
        if not cleaned:
            return _MOUTH_CLOSED
        cleaned = cleaned.replace(" ", "_").replace("-", "_")
        return cleaned

    def _mouth_for_character(self, ch: str) -> str:
        if not ch or ch.isspace():
            return _MOUTH_CLOSED
        if ch in ",;:()[]{}":
            return "mouth_closed"
        if ch in ".!?":
            return "mouth_closed"

        lower = ch.lower()
        if lower in "aeiouy":
            return "mouth_wide_open_4"
        if lower in "mnprb":
            return "mouth_medium"
        if lower in "fsvz":
            return "mouth_narrow"
        if lower in "lk":
            return "mouth_wide_open_2"
        if lower in "dtgq":
            return "mouth_wide_open_1"
        return "mouth_medium"
