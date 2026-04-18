from __future__ import annotations

import heapq
from pathlib import Path
import random
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Deque, Dict, List, Optional, Protocol, Tuple

from speech_controller import SpeechController


class PresenceState(Enum):
    hidden = auto()
    showing = auto()
    visible = auto()
    hiding = auto()


class InteractionMode(Enum):
    normal = auto()
    dragging = auto()
    menu_open = auto()


class BehaviorState(Enum):
    idle = auto()
    idle_level_1 = auto()
    idle_level_2 = auto()
    idle_level_3 = auto()
    reacting = auto()
    speaking = auto()
    listening = auto()
    hearing = auto()
    thinking = auto()
    moving = auto()


class EventType(Enum):
    SHOW_REQUEST = auto()
    HIDE_REQUEST = auto()
    CLICK = auto()
    DOUBLE_CLICK = auto()
    RIGHT_CLICK = auto()
    DRAG_START = auto()
    DRAG_MOVE = auto()
    DRAG_END = auto()
    MENU_CLOSE = auto()
    MENU_COMMAND = auto()
    KEY_INPUT = auto()
    LISTEN_START = auto()
    LISTEN_CANCEL = auto()
    STOP_REQUEST = auto()
    SAY_REQUEST = auto()
    MOVE_REQUEST = auto()
    TASK_START = auto()
    TASK_END = auto()
    AI_REPLY_READY = auto()
    THINK_START = auto()
    THINK_CANCEL = auto()
    ANIMATION_FINISHED = auto()
    AUDIO_FINISHED = auto()
    MOVE_FINISHED = auto()
    SHOW_FINISHED = auto()
    HIDE_FINISHED = auto()
    RUNTIME_ERROR = auto()
    IDLE_TIMEOUT = auto()
    TICK = auto()


class ActionType(Enum):
    PLAY_ANIMATION = auto()
    SYNC_ANIMATION = auto()
    SAY = auto()
    MOVE_TO = auto()
    STOP_CURRENT_ACTION = auto()
    OPEN_MENU = auto()
    CLOSE_MENU = auto()
    SET_OVERLAY = auto()
    SHOW = auto()
    HIDE = auto()
    RESET_IDLE_TIMER = auto()
    CANCEL_SPEECH = auto()


class RuntimeAdapterProtocol(Protocol):
    def play(self, animation_name, frames, fps=10.0, with_audio=False, synced=False, timeline_entries=None, balloon_text=None, loop=True, on_complete=None): ...
    def say(self, text): ...
    def stop(self): ...
    def stop_current_action(self): ...
    def move(self, x, y): ...
    def set_overlay(self, enabled, x=None, y=None): ...
    def set_balloon_text_progress(self, text: str) -> None: ...
    def set_mouth_overlay(self, mouth) -> None: ...
    def clear_speech_visuals(self) -> None: ...
    def status(self): ...
    def available_animation_names(self): ...
    def available_state_names(self): ...
    def render_animation(self, animation_name): ...
    def animation_timeline(self, animation_name): ...
    def animation_analysis(self, animation_name): ...
    def find_animation(self, animation_name): ...


@dataclass(order=True)
class QueuedEvent:
    sort_index: Tuple[int, float, int] = field(init=False, repr=False)
    priority: int
    timestamp: float
    sequence: int
    event_type: EventType = field(compare=False)
    payload: Dict[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self):
        self.sort_index = (-int(self.priority), float(self.timestamp), int(self.sequence))


@dataclass
class ClippyAction:
    action_type: ActionType
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AnimationCaps:
    name: str
    assigned_state: Optional[str] = None
    supports_speaking: bool = False
    looping: bool = False
    return_animation: Optional[str] = None
    uses_exit_branches: bool = False
    has_sound_effects: bool = False
    frame_count: int = 0
    total_duration_ms: int = 0
    transition_type: Optional[str] = None


@dataclass(frozen=True)
class InteractionProfile:
    name: str
    click_behavior: BehaviorState = BehaviorState.reacting
    click_animations: Tuple[str, ...] = ("Acknowledge", "Announce", "GetAttention", "Blink")
    click_text: Optional[str] = None
    click_sequence: Tuple[str, ...] = ()
    post_click_behavior: Optional[BehaviorState] = None
    double_click_behavior: BehaviorState = BehaviorState.reacting
    double_click_animations: Tuple[str, ...] = ("Explain", "Announce", "GetAttention", "Acknowledge")
    double_click_text: Optional[str] = None
    listen_on_click: bool = False


@dataclass
class ClippyContext:
    presence: PresenceState = PresenceState.hidden
    interaction_mode: InteractionMode = InteractionMode.normal
    behavior: BehaviorState = BehaviorState.idle
    previous_behavior: Optional[BehaviorState] = None
    position: Tuple[int, int] = (0, 0)
    target_position: Optional[Tuple[int, int]] = None
    last_animation: Optional[str] = None
    last_reaction: Optional[str] = None
    interaction_profile: str = "default"
    last_user_event: Optional[str] = None
    task_running: bool = False
    speech_pending: bool = False
    speech_active: bool = False
    move_pending: bool = False
    menu_open: bool = False
    dragging: bool = False
    active_request_id: Optional[str] = None
    cancel_requested: bool = False
    idle_level: int = 0
    idle_deadline: float = 0.0
    action_deadline: float = 0.0
    speech_deadline: float = 0.0
    move_deadline: float = 0.0
    pending_actions: Deque[ClippyAction] = field(default_factory=deque)
    recent_animations: Deque[str] = field(default_factory=lambda: deque(maxlen=4))
    recent_idle_animations: Deque[str] = field(default_factory=lambda: deque(maxlen=4))
    current_request_kind: Optional[str] = None
    current_request_payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EventSpec:
    event_type: EventType
    payload: Optional[Dict[str, Any]] = None
    priority: Optional[int] = None
    timestamp: Optional[float] = None


STATE_MAP: Dict[str, Dict[str, Any]] = {
    "idle": {"acs_states": ["Idle", "Blink"], "cooldown": 2.0, "prefer_subtle": True},
    "idle_level_1": {"acs_states": ["IdlingLevel1"], "cooldown": 2.5, "prefer_subtle": True},
    "idle_level_2": {"acs_states": ["IdlingLevel2"], "cooldown": 4.0, "prefer_subtle": True},
    "idle_level_3": {"acs_states": ["IdlingLevel3"], "cooldown": 6.0, "prefer_subtle": True},
    "speaking": {"acs_states": ["Speaking"], "fallback": ["Acknowledge", "Blink"]},
    "reacting": {"acs_states": ["Acknowledge", "Announce", "Confused", "DontRecognize"]},
    "thinking": {"acs_states": ["Explain", "Alert", "Idle"], "fallback": ["Blink"]},
    "moving": {"acs_states": ["MovingLeft", "MovingRight", "MovingUp", "MovingDown"]},
    "listening": {"acs_states": ["Listening"]},
    "hearing": {"acs_states": ["Hearing"]},
    "showing": {"acs_states": ["Showing"]},
    "hidden": {"acs_states": ["Hiding"]},
}


INTERACTION_PROFILES: Dict[str, InteractionProfile] = {
    "default": InteractionProfile(
        name="default",
        click_behavior=BehaviorState.reacting,
        click_animations=("Acknowledge", "Announce", "GetAttention", "Blink"),
        click_text="Bonjour !",
        double_click_behavior=BehaviorState.reacting,
        double_click_animations=("Explain", "Announce", "GetAttention", "Acknowledge"),
        double_click_text="Je t'ecoute.",
    ),
    "merlin": InteractionProfile(
        name="merlin",
        click_behavior=BehaviorState.reacting,
        click_animations=("GetAttention", "Explain"),
        click_text="Je t'ecoute.",
        click_sequence=("GetAttention", "Explain"),
        post_click_behavior=BehaviorState.listening,
        double_click_behavior=BehaviorState.reacting,
        double_click_animations=("Explain", "Announce", "GetAttention", "Acknowledge"),
        double_click_text="Je t'ecoute.",
        listen_on_click=True,
    ),
}


DEFAULT_PRIORITY = {
    EventType.DRAG_START: 100,
    EventType.HIDE_REQUEST: 100,
    EventType.STOP_REQUEST: 100,
    EventType.RIGHT_CLICK: 90,
    EventType.MENU_CLOSE: 90,
    EventType.MENU_COMMAND: 80,
    EventType.CLICK: 70,
    EventType.DOUBLE_CLICK: 70,
    EventType.SAY_REQUEST: 70,
    EventType.MOVE_REQUEST: 70,
    EventType.KEY_INPUT: 70,
    EventType.TASK_START: 60,
    EventType.TASK_END: 60,
    EventType.AI_REPLY_READY: 60,
    EventType.THINK_START: 60,
    EventType.THINK_CANCEL: 60,
    EventType.SHOW_REQUEST: 55,
    EventType.LISTEN_START: 55,
    EventType.LISTEN_CANCEL: 55,
    EventType.DRAG_MOVE: 40,
    EventType.DRAG_END: 40,
    EventType.ANIMATION_FINISHED: 30,
    EventType.AUDIO_FINISHED: 30,
    EventType.MOVE_FINISHED: 30,
    EventType.SHOW_FINISHED: 30,
    EventType.HIDE_FINISHED: 30,
    EventType.RUNTIME_ERROR: 30,
    EventType.IDLE_TIMEOUT: 10,
    EventType.TICK: 1,
}


class RuntimeAdapter:
    def __init__(self, runtime: RuntimeAdapterProtocol):
        self.runtime = runtime

    def __getattr__(self, name):
        return getattr(self.runtime, name)

    def play_animation(
        self,
        animation_name: str,
        *,
        with_audio: bool = False,
        synced: bool = False,
        balloon_text: Optional[str] = None,
        loop: bool = False,
        on_complete=None,
        fps: float = 10.0,
    ) -> str:
        animation, frames, _ = self.runtime.render_animation(animation_name)
        timeline_entries = None
        if synced:
            _, timeline_entries, _ = self.runtime.animation_timeline(animation_name)
        request_id = uuid.uuid4().hex
        self.runtime.play(
            animation.name,
            frames,
            fps=fps,
            with_audio=with_audio,
            synced=synced,
            timeline_entries=timeline_entries,
            balloon_text=balloon_text,
            loop=loop,
            on_complete=on_complete,
        )
        return request_id

    def say(self, text: str):
        self.runtime.say(text)

    def stop(self):
        stop_current_action = getattr(self.runtime, "stop_current_action", None)
        if callable(stop_current_action):
            stop_current_action()
            return
        self.runtime.stop()

    def move(self, x: int, y: int):
        self.runtime.move(x, y)

    def set_overlay(self, enabled: bool, x=None, y=None):
        self.runtime.set_overlay(enabled, x=x, y=y)

    def set_balloon_text_progress(self, text: str) -> None:
        self.runtime.set_balloon_text_progress(text)

    def set_mouth_overlay(self, mouth) -> None:
        self.runtime.set_mouth_overlay(mouth)

    def clear_speech_visuals(self) -> None:
        self.runtime.clear_speech_visuals()

    def status(self):
        return self.runtime.status()

    def available_animation_names(self):
        return self.runtime.available_animation_names()

    def available_state_names(self):
        return self.runtime.available_state_names()

    def find_animation(self, animation_name: str):
        return self.runtime.find_animation(animation_name)

    def render_animation(self, animation_name: str):
        return self.runtime.render_animation(animation_name)

    def animation_timeline(self, animation_name: str):
        return self.runtime.animation_timeline(animation_name)

    def animation_analysis(self, animation_name: str):
        return self.runtime.animation_analysis(animation_name)


class RuleBasedDecisionLayer:
    def __init__(self, rng: Optional[random.Random] = None):
        self.rng = rng or random.Random()

    def choose_animation_names(self, fsm: "ClippyStateMachine", behavior_key: str, event: Optional[QueuedEvent] = None) -> List[str]:
        payload = (event.payload if event else {}) or {}
        explicit = payload.get("preferred_animations")
        if explicit:
            return [str(name) for name in explicit]
        profile = fsm.interaction_profile
        if event is not None and event.event_type == EventType.CLICK:
            names = list(profile.click_animations)
            if profile.click_sequence:
                names = [profile.click_sequence[0]]
            return names
        if event is not None and event.event_type == EventType.DOUBLE_CLICK:
            return list(profile.double_click_animations)
        state_map = STATE_MAP.get(behavior_key, {})
        preferred = list(state_map.get("acs_states", []))
        fallback = list(state_map.get("fallback", []))
        if behavior_key == "moving":
            target = payload.get("target_position")
            if target:
                preferred = self._directional_motion_choice(fsm, target) or preferred
        return preferred + [name for name in fallback if name not in preferred]

    def choose_text(self, fsm: "ClippyStateMachine", event: QueuedEvent) -> Optional[str]:
        payload = event.payload or {}
        text = payload.get("text")
        if text is not None:
            return str(text)
        profile = fsm.interaction_profile
        if event.event_type == EventType.CLICK:
            return profile.click_text
        if event.event_type == EventType.DOUBLE_CLICK:
            return profile.double_click_text
        return None

    def _directional_motion_choice(self, fsm: "ClippyStateMachine", target_position):
        if not target_position:
            return None
        try:
            cur_x, cur_y = fsm.context.position
            target_x, target_y = target_position
        except Exception:
            return None
        dx = int(target_x) - int(cur_x)
        dy = int(target_y) - int(cur_y)
        if abs(dx) >= abs(dy):
            return ["MovingRight"] if dx >= 0 else ["MovingLeft"]
        return ["MovingDown"] if dy >= 0 else ["MovingUp"]


class ClippyStateMachine:
    def __init__(self, runtime: RuntimeAdapterProtocol, rng: Optional[random.Random] = None):
        self.runtime = RuntimeAdapter(runtime)
        self.rng = rng or random.Random()
        self.policy = RuleBasedDecisionLayer(self.rng)
        self.context = ClippyContext()
        self.interaction_profile = self._resolve_interaction_profile()
        self._event_queue: List[QueuedEvent] = []
        self._sequence = 0
        self._animations = self._build_animation_catalog()
        self._state_names = {name.casefold(): name for name in self.runtime.available_state_names()}
        self._animation_names = {name.casefold(): name for name in self.runtime.available_animation_names()}
        self.context.position = self._safe_position_from_runtime()
        self._speech_controller: Optional[SpeechController] = None
        self._speech_text: Optional[str] = None
        self._speech_started_at: Optional[float] = None
        self._last_tick_time = time.monotonic()
        self.reset_idle_timer(jitter=False)

    def _debug_enabled(self) -> bool:
        player = getattr(self.runtime, "player", None)
        return bool(getattr(player, "_service_debug", False))

    def _resolve_interaction_profile(self) -> InteractionProfile:
        character_path = getattr(self.runtime, "character_path", None)
        character_name = ""
        if character_path is not None:
            try:
                character_name = Path(str(character_path)).stem.casefold()
            except Exception:
                character_name = str(character_path).casefold()
        profile = INTERACTION_PROFILES.get(character_name)
        if profile is None:
            profile = INTERACTION_PROFILES["default"]
        self.context.interaction_profile = profile.name
        return profile

    def _safe_position_from_runtime(self) -> Tuple[int, int]:
        try:
            status = self.runtime.status() or {}
        except Exception:
            return (0, 0)
        x = status.get("x")
        y = status.get("y")
        if x is None or y is None:
            return (0, 0)
        try:
            return (int(x), int(y))
        except Exception:
            return (0, 0)

    def _build_animation_catalog(self) -> Dict[str, AnimationCaps]:
        catalog: Dict[str, AnimationCaps] = {}
        for name in self.runtime.available_animation_names():
            analysis = None
            try:
                analysis = self.runtime.animation_analysis(name)
            except Exception:
                analysis = None
            return_animation = None
            transition_type = None
            frame_count = 0
            total_duration_ms = 0
            supports_speaking = False
            looping = False
            uses_exit_branches = False
            has_sound_effects = False
            if analysis and isinstance(analysis, dict):
                animation = analysis.get("animation")
                if animation is not None:
                    data = getattr(animation, "animation_data", None)
                    if data is not None:
                        return_animation = getattr(data, "return_animation", None) or None
                        transition_type = getattr(data, "transition_type", None) or None
                        frames = list(getattr(data, "frames", []) or [])
                        frame_count = len(frames)
                        total_duration_ms = sum(max(1, int(round((getattr(frame, "frame_duration_csecs", 1) or 1) * 10))) for frame in frames)
                        frames_with_audio = [frame for frame in frames if getattr(frame, "audio_index", 65535) not in (None, 65535)]
                        has_sound_effects = bool(frames_with_audio)
                        supports_speaking = bool(frames_with_audio or any(getattr(frame, "mouth_overlays", []) for frame in frames))
                        uses_exit_branches = transition_type == "use_exit_branches" or any(
                            getattr(frame, "exit_to_frame_index", -1) not in (None, -1) for frame in frames
                        )
                        looping = bool(return_animation) or transition_type == "use_return_animation"
            assigned_state = None
            try:
                for state in getattr(self.runtime, "list_states", lambda: [])():
                    for anim_name in getattr(state, "animations", []) or []:
                        if str(anim_name).casefold() == name.casefold():
                            assigned_state = getattr(state, "name", None)
                            break
                    if assigned_state:
                        break
            except Exception:
                assigned_state = None
            catalog[name.casefold()] = AnimationCaps(
                name=name,
                assigned_state=assigned_state,
                supports_speaking=supports_speaking,
                looping=looping,
                return_animation=return_animation,
                uses_exit_branches=uses_exit_branches,
                has_sound_effects=has_sound_effects,
                frame_count=frame_count,
                total_duration_ms=total_duration_ms,
                transition_type=transition_type,
            )
        return catalog

    def post_event(self, event_type: EventType, payload: Optional[Dict[str, Any]] = None, priority: Optional[int] = None, timestamp: Optional[float] = None):
        payload = dict(payload or {})
        if event_type == EventType.STOP_REQUEST:
            source = payload.get("source", "unknown")
            if self.context.cancel_requested:
                print(f"[fsm] skip duplicate STOP_REQUEST source={source} reason=cancel_requested")
                return
            if any(event.event_type == EventType.STOP_REQUEST for event in self._event_queue):
                print(f"[fsm] skip duplicate STOP_REQUEST source={source} reason=queued")
                return
        self._sequence += 1
        queued = QueuedEvent(
            priority=DEFAULT_PRIORITY.get(event_type, 0) if priority is None else int(priority),
            timestamp=time.monotonic() if timestamp is None else float(timestamp),
            sequence=self._sequence,
            event_type=event_type,
            payload=payload,
        )
        heapq.heappush(self._event_queue, queued)
        if len(self._event_queue) > 5000:
            dominant = Counter(event.event_type.name for event in self._event_queue).most_common(5)
            print(f"[fsm] queue warning size={len(self._event_queue)} dominant={dominant}")
        if event_type == EventType.STOP_REQUEST:
            print(f"[fsm] enqueue STOP_REQUEST source={payload.get('source', 'unknown')} queue={len(self._event_queue)}")
        elif self._debug_enabled() and event_type != EventType.TICK:
            print(f"[fsm] enqueue {event_type.name} payload_keys={sorted(payload.keys())}")

    def on_runtime_event(self, event_type: EventType, payload: Optional[Dict[str, Any]] = None):
        self.post_event(event_type, payload=payload)

    def reset_idle_timer(self, jitter: bool = True):
        base = 2.5
        if jitter:
            base += self.rng.uniform(-0.5, 0.75)
        self.context.idle_deadline = time.monotonic() + max(0.5, base)

    def tick(self, now: Optional[float] = None):
        now = time.monotonic() if now is None else float(now)
        dt = max(0.0, now - self._last_tick_time)
        self._last_tick_time = now

        if self.context.behavior == BehaviorState.speaking:
            self._update_speech_controller(dt, now)

        if now >= self.context.idle_deadline and self.context.behavior in {
            BehaviorState.idle,
            BehaviorState.idle_level_1,
            BehaviorState.idle_level_2,
            BehaviorState.idle_level_3,
        }:
            self.post_event(EventType.IDLE_TIMEOUT, priority=DEFAULT_PRIORITY[EventType.IDLE_TIMEOUT], timestamp=now)
            self.reset_idle_timer()
        if self.context.active_request_id and self.context.action_deadline and now >= self.context.action_deadline:
            kind = self.context.current_request_kind
            if kind == "speech":
                self.on_runtime_event(EventType.AUDIO_FINISHED, {"request_id": self.context.active_request_id})
            else:
                self.on_runtime_event(EventType.ANIMATION_FINISHED, {"request_id": self.context.active_request_id})
            self.context.action_deadline = 0.0
        processed = 0
        while self._event_queue and processed < 100:
            event = heapq.heappop(self._event_queue)
            processed += 1
            self._handle_event(event)
        self._drain_pending_actions()

    def _handle_event(self, event: QueuedEvent):
        payload = event.payload or {}
        event_name = event.event_type.name.lower()
        if event.event_type in {
            EventType.CLICK,
            EventType.DOUBLE_CLICK,
            EventType.KEY_INPUT,
            EventType.SAY_REQUEST,
            EventType.MOVE_REQUEST,
            EventType.TASK_START,
            EventType.TASK_END,
            EventType.AI_REPLY_READY,
            EventType.THINK_START,
            EventType.THINK_CANCEL,
            EventType.LISTEN_START,
            EventType.LISTEN_CANCEL,
            EventType.SHOW_REQUEST,
            EventType.HIDE_REQUEST,
            EventType.STOP_REQUEST,
        }:
            self.context.last_user_event = event_name
        if self._debug_enabled() and event.event_type in {EventType.CLICK, EventType.DOUBLE_CLICK, EventType.LISTEN_START, EventType.LISTEN_CANCEL, EventType.SAY_REQUEST}:
            source = payload.get("source", "unknown")
            if event.event_type == EventType.CLICK:
                print(f"[fsm] SAY_HELLO source={source} profile={self.interaction_profile.name}")
            elif event.event_type == EventType.DOUBLE_CLICK:
                print(f"[fsm] LISTEN_REQUEST source={source} profile={self.interaction_profile.name}")
            elif event.event_type == EventType.LISTEN_START:
                print(f"[fsm] LISTEN_REQUEST source={source} profile={self.interaction_profile.name}")
            elif event.event_type == EventType.LISTEN_CANCEL:
                print(f"[fsm] LISTEN_CANCEL source={source}")
            elif event.event_type == EventType.SAY_REQUEST:
                print(f"[fsm] SAY_REQUEST source={source}")

        if event.event_type == EventType.TICK:
            return
        if event.event_type == EventType.SHOW_REQUEST:
            self._enter_presence(PresenceState.showing)
            self._queue_action(ClippyAction(ActionType.SET_OVERLAY, {"enabled": True, "x": payload.get("x"), "y": payload.get("y")}))
            self._queue_action(ClippyAction(ActionType.PLAY_ANIMATION, {
                "animation_name": self._choose_behavior_animation("showing", event),
                "behavior": "showing",
                "request_id": self._new_request_id(),
                "loop": False,
            }))
            self.reset_idle_timer()
            return
        if event.event_type == EventType.HIDE_REQUEST:
            self.context.cancel_requested = True
            self._queue_action(ClippyAction(ActionType.STOP_CURRENT_ACTION, {}))
            self._enter_presence(PresenceState.hiding)
            self._queue_action(ClippyAction(ActionType.PLAY_ANIMATION, {
                "animation_name": self._choose_behavior_animation("hidden", event),
                "behavior": "hiding",
                "request_id": self._new_request_id(),
                "loop": False,
            }))
            self.reset_idle_timer()
            return
        if event.event_type in {EventType.CLICK, EventType.DOUBLE_CLICK}:
            self._set_visible_if_needed()
            profile = self.interaction_profile
            click_behavior = profile.click_behavior if event.event_type == EventType.CLICK else profile.double_click_behavior
            self._transition_behavior(click_behavior)
            payload = dict(payload)
            if event.event_type == EventType.CLICK:
                payload.setdefault("text", self.policy.choose_text(self, event))
                if profile.click_sequence:
                    payload.setdefault("click_sequence", list(profile.click_sequence))
                    payload.setdefault("post_click_behavior", profile.post_click_behavior.name if profile.post_click_behavior else None)
                    payload.setdefault("preferred_animations", list(profile.click_sequence[:1]))
                else:
                    payload.setdefault("preferred_animations", list(profile.click_animations))
            else:
                payload.setdefault("preferred_animations", list(profile.double_click_animations))
                payload.setdefault("text", self.policy.choose_text(self, event))
            animation_name = None
            if event.event_type == EventType.CLICK and profile.click_sequence:
                animation_name = self._resolve_animation_name(str(profile.click_sequence[0]))
                if animation_name is None:
                    animation_name = self._choose_behavior_animation(click_behavior.name, event)
            else:
                animation_name = self._choose_behavior_animation(click_behavior.name, event)
            self._queue_action(ClippyAction(ActionType.PLAY_ANIMATION, {
                "animation_name": animation_name,
                "behavior": click_behavior.name,
                "request_id": self._new_request_id(),
                "loop": False,
                "balloon_text": payload.get("text"),
                "click_sequence": list(payload.get("click_sequence") or []),
                "post_click_behavior": payload.get("post_click_behavior"),
            }))
            if payload.get("text"):
                self._queue_action(ClippyAction(ActionType.SAY, {"text": payload.get("text"), "behavior": "speaking", "source": payload.get("source", "unknown")}))
            self.reset_idle_timer()
            return
        if event.event_type == EventType.RIGHT_CLICK:
            self.context.menu_open = True
            self.context.interaction_mode = InteractionMode.menu_open
            self._queue_action(ClippyAction(ActionType.OPEN_MENU, {"event": event.payload}))
            return
        if event.event_type == EventType.MENU_CLOSE:
            self.context.menu_open = False
            self.context.interaction_mode = InteractionMode.normal
            self._queue_action(ClippyAction(ActionType.CLOSE_MENU, {}))
            self.reset_idle_timer()
            return
        if event.event_type == EventType.MENU_COMMAND:
            self.context.menu_open = False
            self.context.interaction_mode = InteractionMode.normal
            command = payload.get("command")
            if command:
                self._handle_menu_command(command, payload)
            self.reset_idle_timer()
            return
        if event.event_type == EventType.DRAG_START:
            self.context.dragging = True
            self.context.interaction_mode = InteractionMode.dragging
            self.context.cancel_requested = True
            self._queue_action(ClippyAction(ActionType.STOP_CURRENT_ACTION, {}))
            self._queue_action(ClippyAction(ActionType.CANCEL_SPEECH, {}))
            return
        if event.event_type == EventType.DRAG_MOVE:
            pos = payload.get("position")
            if pos:
                self.context.position = (int(pos[0]), int(pos[1]))
            return
        if event.event_type == EventType.DRAG_END:
            self.context.dragging = False
            self.context.interaction_mode = InteractionMode.normal
            self._transition_behavior(BehaviorState.idle)
            self.reset_idle_timer()
            return
        if event.event_type == EventType.SAY_REQUEST:
            text = payload.get("text")
            if text is None:
                return
            print(f"[fsm] SAY_REQUEST received: {text!r}")
            self.context.speech_pending = True
            self._begin_speech(str(text))
            self._transition_behavior(BehaviorState.speaking)
            self._queue_action(ClippyAction(ActionType.SAY, {"text": str(text), "behavior": "speaking", "request_id": self._new_request_id(), "balloon_text": str(text)}))
            return
        if event.event_type == EventType.MOVE_REQUEST:
            target = payload.get("position")
            if not target:
                return
            self.context.move_pending = True
            self.context.target_position = (int(target[0]), int(target[1]))
            self._transition_behavior(BehaviorState.moving)
            self._queue_action(ClippyAction(ActionType.MOVE_TO, {"position": self.context.target_position, "request_id": self._new_request_id()}))
            return
        if event.event_type == EventType.TASK_START:
            self.context.task_running = True
            self._transition_behavior(BehaviorState.thinking)
            return
        if event.event_type == EventType.TASK_END:
            self.context.task_running = False
            if self.context.behavior == BehaviorState.thinking:
                self._transition_behavior(BehaviorState.idle)
            return
        if event.event_type == EventType.AI_REPLY_READY:
            text = payload.get("text")
            if text:
                self.post_event(EventType.SAY_REQUEST, {"text": text})
            return
        if event.event_type == EventType.THINK_START:
            self.context.task_running = True
            self._transition_behavior(BehaviorState.thinking)
            return
        if event.event_type == EventType.THINK_CANCEL:
            self.context.task_running = False
            self._transition_behavior(BehaviorState.idle)
            return
        if event.event_type == EventType.LISTEN_START:
            self._transition_behavior(BehaviorState.listening)
            self._queue_action(ClippyAction(ActionType.PLAY_ANIMATION, {
                "animation_name": self._choose_behavior_animation("listening", event),
                "behavior": "listening",
                "request_id": self._new_request_id(),
                "loop": False,
            }))
            self.reset_idle_timer()
            return
        if event.event_type == EventType.LISTEN_CANCEL:
            self._transition_behavior(BehaviorState.idle)
            return
        if event.event_type == EventType.STOP_REQUEST:
            self.context.cancel_requested = True
            self._queue_action(ClippyAction(ActionType.STOP_CURRENT_ACTION, {"source": event.payload.get("source", "unknown")}))
            self._clear_speech_controller(clear_visuals=True)
            self._transition_behavior(BehaviorState.idle)
            return
        if event.event_type == EventType.IDLE_TIMEOUT:
            self._advance_idle()
            return
        if event.event_type == EventType.ANIMATION_FINISHED:
            self._on_animation_finished(event)
            return
        if event.event_type == EventType.AUDIO_FINISHED:
            self._on_audio_finished(event)
            return
        if event.event_type == EventType.MOVE_FINISHED:
            self._on_move_finished(event)
            return
        if event.event_type == EventType.SHOW_FINISHED:
            self._enter_presence(PresenceState.visible)
            self._transition_behavior(BehaviorState.idle)
            return
        if event.event_type == EventType.HIDE_FINISHED:
            self._enter_presence(PresenceState.hidden)
            self._transition_behavior(BehaviorState.idle)
            self.runtime.set_overlay(False, x=None, y=None)
            return
        if event.event_type == EventType.RUNTIME_ERROR:
            self.context.cancel_requested = True
            self.context.active_request_id = None
            self._transition_behavior(BehaviorState.idle)
            return

    def _handle_menu_command(self, command: str, payload: Dict[str, Any]):
        command = str(command).strip().lower()
        if command == "say":
            self.post_event(EventType.SAY_REQUEST, {"text": payload.get("text", "")})
        elif command == "move":
            self.post_event(EventType.MOVE_REQUEST, {"position": payload.get("position", self.context.position)})
        elif command == "show":
            self.post_event(EventType.SHOW_REQUEST, payload)
        elif command == "hide":
            self.post_event(EventType.HIDE_REQUEST, payload)
        elif command == "stop":
            self.post_event(EventType.STOP_REQUEST, {**payload, "source": "menu_stop"})

    def _reaction_candidates(self, event_type: EventType) -> List[str]:
        if event_type == EventType.DOUBLE_CLICK:
            return ["Explain", "Announce", "GetAttention", "Acknowledge"]
        return ["Acknowledge", "Announce", "GetAttention", "Blink"]

    def _resolve_lipsync_source(self):
        for candidate in (
            getattr(self.runtime, "lipsync", None),
            getattr(getattr(self.runtime, "catalog_runtime", None), "lipsync", None),
            getattr(getattr(self.runtime, "player", None), "lipsync", None),
        ):
            if candidate is not None:
                return candidate
        return None

    def _begin_speech(self, text: str):
        self._clear_speech_controller(clear_visuals=True)
        self._speech_text = str(text)
        self._speech_started_at = time.monotonic()
        self.context.speech_pending = True
        self.context.speech_active = True
        self._speech_controller = SpeechController(
            text=self._speech_text,
            lipsync=self._resolve_lipsync_source(),
            chars_per_second=18.0,
        )
        self._speech_controller.start()
        self.runtime.set_balloon_text_progress("")
        self.runtime.set_mouth_overlay(self._speech_controller.current_mouth)

    def _clear_speech_controller(self, *, clear_visuals: bool = False):
        self._speech_controller = None
        self._speech_text = None
        self._speech_started_at = None
        self.context.speech_active = False
        self.context.speech_pending = False
        if clear_visuals:
            self.runtime.clear_speech_visuals()

    def _update_speech_controller(self, dt: float, now: Optional[float] = None):
        controller = self._speech_controller
        if controller is None:
            self.context.speech_active = False
            self.context.speech_pending = False
            self.context.active_request_id = None
            self.context.current_request_kind = None
            self.context.action_deadline = 0.0
            if self.context.behavior == BehaviorState.speaking:
                self._transition_behavior(BehaviorState.idle)
            self.runtime.clear_speech_visuals()
            return

        controller.update(dt)
        self.runtime.set_balloon_text_progress(controller.visible_text)
        self.runtime.set_mouth_overlay(controller.current_mouth)

        if controller.is_done:
            self.runtime.set_balloon_text_progress(controller.visible_text)
            self.runtime.clear_speech_visuals()
            self.context.speech_active = False
            self.context.speech_pending = False
            self.context.active_request_id = None
            self.context.current_request_kind = None
            self.context.action_deadline = 0.0
            self._clear_speech_controller(clear_visuals=False)
            if self.context.behavior == BehaviorState.speaking:
                self._transition_behavior(BehaviorState.idle)
            return

    def _new_request_id(self) -> str:
        return uuid.uuid4().hex

    def _queue_action(self, action: ClippyAction):
        self.context.pending_actions.append(action)

    def _drain_pending_actions(self):
        if self.context.active_request_id and self.context.current_request_kind in {"speech", "animation", "move"}:
            return
        while self.context.pending_actions:
            action = self.context.pending_actions.popleft()
            if self._dispatch_action(action):
                break

    def _dispatch_action(self, action: ClippyAction) -> bool:
        payload = action.payload or {}
        if action.action_type == ActionType.STOP_CURRENT_ACTION:
            stop_current_action = getattr(self.runtime, "stop_current_action", None)
            if callable(stop_current_action):
                stop_current_action()
            else:
                self.runtime.stop()
            self.context.active_request_id = None
            self.context.current_request_kind = None
            self.context.action_deadline = 0.0
            self.context.move_pending = False
            self._clear_speech_controller(clear_visuals=True)
            return False
        if action.action_type == ActionType.CANCEL_SPEECH:
            stop_current_action = getattr(self.runtime, "stop_current_action", None)
            if callable(stop_current_action):
                stop_current_action()
            else:
                self.runtime.stop()
            self._clear_speech_controller(clear_visuals=True)
            return False
        if action.action_type == ActionType.SET_OVERLAY:
            self.runtime.set_overlay(bool(payload.get("enabled", True)), x=payload.get("x"), y=payload.get("y"))
            return False
        if action.action_type == ActionType.OPEN_MENU:
            self.context.menu_open = True
            self.context.interaction_mode = InteractionMode.menu_open
            return False
        if action.action_type == ActionType.CLOSE_MENU:
            self.context.menu_open = False
            self.context.interaction_mode = InteractionMode.normal
            return False
        if action.action_type == ActionType.SHOW:
            self.runtime.set_overlay(True, x=payload.get("x"), y=payload.get("y"))
            return False
        if action.action_type == ActionType.HIDE:
            self.runtime.set_overlay(False, x=payload.get("x"), y=payload.get("y"))
            return False
        if action.action_type == ActionType.RESET_IDLE_TIMER:
            self.reset_idle_timer()
            return False
        if action.action_type == ActionType.MOVE_TO:
            position = payload.get("position")
            if not position:
                return False
            x, y = int(position[0]), int(position[1])
            self.runtime.move(x, y)
            self.context.position = (x, y)
            self.context.move_pending = False
            self.context.active_request_id = str(payload.get("request_id") or self._new_request_id())
            self.context.current_request_kind = "move"
            self.context.action_deadline = time.monotonic() + 0.02
            self.post_event(EventType.MOVE_FINISHED, {"request_id": self.context.active_request_id, "position": self.context.position}, priority=DEFAULT_PRIORITY[EventType.MOVE_FINISHED], timestamp=time.monotonic() + 0.02)
            return True
        if action.action_type in {ActionType.PLAY_ANIMATION, ActionType.SYNC_ANIMATION}:
            animation_name = action.payload.get("animation_name")
            if not animation_name:
                return False
            animation_name = self._resolve_animation_name(str(animation_name))
            if not animation_name:
                return False
            behavior_key = str(payload.get("behavior") or self.context.behavior.name)
            synced = action.action_type == ActionType.SYNC_ANIMATION or bool(payload.get("synced", False))
            with_audio = bool(payload.get("with_audio", False))
            balloon_text = payload.get("balloon_text")
            loop = bool(payload.get("loop", False))
            request_id = str(payload.get("request_id") or self._new_request_id())
            self.context.active_request_id = request_id
            self.context.current_request_kind = "animation"
            self.context.current_request_payload = dict(payload)
            self.context.last_animation = animation_name
            self.context.recent_animations.append(animation_name)
            if behavior_key in {"idle", "idle_level_1", "idle_level_2", "idle_level_3"}:
                self.context.recent_idle_animations.append(animation_name)
            fps = float(payload.get("fps", 10.0))
            timeline = None
            if synced:
                _, timeline, _ = self.runtime.animation_timeline(animation_name)
            analysis = self._animations.get(animation_name.casefold())
            _ = self._estimate_animation_duration(animation_name, timeline, analysis)
            self.context.action_deadline = 0.0
            self.runtime.play(
                animation_name,
                self.runtime.render_animation(animation_name)[1],
                fps=fps,
                with_audio=with_audio,
                synced=synced,
                timeline_entries=timeline,
                balloon_text=balloon_text,
                loop=loop,
                on_complete=lambda: self.on_runtime_event(
                    EventType.ANIMATION_FINISHED,
                    {"request_id": request_id, "animation_name": animation_name, "behavior": behavior_key},
                ),
            )
            return True
        if action.action_type == ActionType.SAY:
            text = payload.get("text")
            if text is None:
                return False
            text = str(text)
            self.context.speech_pending = True
            self.context.speech_active = True
            request_id = str(payload.get("request_id") or self._new_request_id())
            self.context.active_request_id = request_id
            self.context.current_request_kind = "speech"
            self.context.current_request_payload = dict(payload)
            if self._debug_enabled():
                source = payload.get("source", "unknown")
                print(f"[fsm] balloon_text source={source} text={text!r}")
            self.runtime.say(text)
            self.context.action_deadline = time.monotonic() + self._estimate_speech_duration(text)
            self.post_event(
                EventType.AUDIO_FINISHED,
                {"request_id": request_id, "text": text},
                priority=DEFAULT_PRIORITY[EventType.AUDIO_FINISHED],
                timestamp=self.context.action_deadline,
            )
            return True
        return False

    def _estimate_animation_duration(self, animation_name: str, timeline, caps: Optional[AnimationCaps]) -> float:
        if timeline:
            duration_ms = sum(int(entry.get("duration_ms", 0) or 0) for entry in timeline)
            if duration_ms > 0:
                return max(0.25, min(8.0, duration_ms / 1000.0))
        if caps and caps.total_duration_ms > 0:
            return max(0.25, min(8.0, caps.total_duration_ms / 1000.0))
        try:
            analysis = self.runtime.animation_analysis(animation_name)
            text = str(analysis.get("text", ""))
            for line in text.splitlines():
                if line.startswith("Total duration:"):
                    value = line.split(":", 1)[1].strip().split(" ", 1)[0]
                    return max(0.25, min(8.0, float(value)))
        except Exception:
            pass
        return 0.75

    def _estimate_speech_duration(self, text: str) -> float:
        words = max(1, len(str(text).split()))
        chars = len(str(text))
        return max(0.75, min(6.0, 0.22 * words + 0.01 * chars))

    def _resolve_animation_name(self, animation_name: str) -> Optional[str]:
        if not animation_name:
            return None
        direct = self._animation_names.get(animation_name.casefold())
        if direct:
            return direct
        for name in self._animation_names.values():
            if name.casefold() == animation_name.casefold():
                return name
        return None

    def _choose_behavior_animation(self, behavior_key: str, event: Optional[QueuedEvent] = None) -> Optional[str]:
        candidates = self.policy.choose_animation_names(self, behavior_key, event)
        available = []
        for candidate in candidates:
            resolved = self._resolve_animation_name(candidate)
            if resolved:
                available.append(resolved)
        if not available:
            return None
        if behavior_key in {"idle", "idle_level_1", "idle_level_2", "idle_level_3"}:
            filtered = [name for name in available if name not in list(self.context.recent_idle_animations)[-2:]]
            if filtered:
                available = filtered
        else:
            filtered = [name for name in available if name not in list(self.context.recent_animations)[-2:]]
            if filtered:
                available = filtered
        return self.rng.choice(available)

    def _transition_behavior(self, next_behavior: BehaviorState):
        if self.context.behavior != next_behavior:
            if self._debug_enabled():
                print(f"[fsm] behavior {self.context.behavior.name} -> {next_behavior.name}")
            self.context.previous_behavior = self.context.behavior
            self.context.behavior = next_behavior

    def _enter_presence(self, next_presence: PresenceState):
        if self.context.presence != next_presence and self._debug_enabled():
            print(f"[fsm] presence {self.context.presence.name} -> {next_presence.name}")
        self.context.presence = next_presence

    def _set_visible_if_needed(self):
        if self.context.presence == PresenceState.hidden:
            self._enter_presence(PresenceState.visible)
            desired_overlay = False
            try:
                status = self.runtime.status() or {}
                desired_overlay = bool(status.get("overlay", False))
            except Exception:
                desired_overlay = False
            self.runtime.set_overlay(desired_overlay, x=self.context.position[0], y=self.context.position[1])

    def _advance_idle(self):
        if self.context.dragging or self.context.menu_open or self.context.task_running:
            return
        level = min(3, max(0, int(self.context.idle_level) + 1))
        self.context.idle_level = level
        if level <= 0:
            self._transition_behavior(BehaviorState.idle)
            return
        if level == 1:
            self._transition_behavior(BehaviorState.idle_level_1)
        elif level == 2:
            self._transition_behavior(BehaviorState.idle_level_2)
        else:
            self._transition_behavior(BehaviorState.idle_level_3)
        animation = self._choose_behavior_animation(self.context.behavior.name)
        if animation:
            self._queue_action(ClippyAction(ActionType.PLAY_ANIMATION, {
                "animation_name": animation,
                "behavior": self.context.behavior.name,
                "request_id": self._new_request_id(),
                "loop": False,
            }))
        cooldown = STATE_MAP.get(self.context.behavior.name, {}).get("cooldown", 2.5)
        jitter = self.rng.uniform(-0.35, 0.75)
        self.context.idle_deadline = time.monotonic() + max(0.8, float(cooldown) + jitter)

    def _on_animation_finished(self, event: QueuedEvent):
        payload = event.payload or {}
        if self.context.active_request_id and payload.get("request_id") not in (None, self.context.active_request_id):
            return
        current_payload = dict(self.context.current_request_payload or {})
        kind = self.context.current_request_kind
        click_sequence = list(current_payload.get("click_sequence") or [])
        if kind == "animation" and click_sequence:
            if len(click_sequence) > 1:
                next_sequence = click_sequence[1:]
                next_animation_name = self._resolve_animation_name(next_sequence[0]) if next_sequence else None
                if next_animation_name:
                    self.context.current_request_payload["click_sequence"] = next_sequence
                    self._queue_action(ClippyAction(ActionType.PLAY_ANIMATION, {
                        "animation_name": next_animation_name,
                        "behavior": current_payload.get("behavior", self.context.behavior.name),
                        "request_id": self.context.active_request_id or self._new_request_id(),
                        "loop": False,
                        "balloon_text": None,
                        "click_sequence": next_sequence,
                        "post_click_behavior": current_payload.get("post_click_behavior"),
                    }))
                    return
            post_click_behavior = current_payload.get("post_click_behavior")
            self.context.active_request_id = None
            self.context.current_request_kind = None
            self.context.current_request_payload = {}
            if post_click_behavior == "listening":
                self._transition_behavior(BehaviorState.listening)
            elif post_click_behavior == "reacting":
                self._transition_behavior(BehaviorState.reacting)
            else:
                if self.context.behavior in {BehaviorState.reacting, BehaviorState.listening}:
                    self._transition_behavior(BehaviorState.idle)
            return
        self.context.active_request_id = None
        self.context.current_request_kind = None
        if kind == "move":
            self.context.move_pending = False
            self._transition_behavior(BehaviorState.idle)
            self.post_event(EventType.MOVE_FINISHED, payload)
            return
        if self.context.presence == PresenceState.showing:
            self._enter_presence(PresenceState.visible)
            self._transition_behavior(BehaviorState.idle)
            self.post_event(EventType.SHOW_FINISHED, payload)
            return
        if self.context.presence == PresenceState.hiding:
            self._enter_presence(PresenceState.hidden)
            self._transition_behavior(BehaviorState.idle)
            self.runtime.set_overlay(False, x=None, y=None)
            self.post_event(EventType.HIDE_FINISHED, payload)
            return
        if self.context.behavior == BehaviorState.reacting:
            self._transition_behavior(BehaviorState.idle)
        elif self.context.behavior == BehaviorState.listening:
            self._transition_behavior(BehaviorState.hearing)
            hearing = self._choose_behavior_animation("hearing")
            if hearing:
                self._queue_action(ClippyAction(ActionType.PLAY_ANIMATION, {
                    "animation_name": hearing,
                    "behavior": "hearing",
                    "request_id": self._new_request_id(),
                    "loop": False,
                }))
        elif self.context.behavior == BehaviorState.speaking:
            self._transition_behavior(BehaviorState.idle)
            self.context.speech_active = False
            self.context.speech_pending = False
        elif self.context.behavior == BehaviorState.thinking:
            if not self.context.task_running:
                self._transition_behavior(BehaviorState.idle)

    def _on_audio_finished(self, event: QueuedEvent):
        payload = event.payload or {}
        if self.context.active_request_id and payload.get("request_id") not in (None, self.context.active_request_id):
            return
        if self._speech_controller is not None and not self._speech_controller.is_done:
            return
        self.context.active_request_id = None
        self.context.current_request_kind = None
        self.context.action_deadline = 0.0
        self._clear_speech_controller(clear_visuals=True)
        self._transition_behavior(BehaviorState.idle)

    def _on_move_finished(self, event: QueuedEvent):
        payload = event.payload or {}
        if self.context.active_request_id and payload.get("request_id") not in (None, self.context.active_request_id):
            return
        self.context.active_request_id = None
        self.context.current_request_kind = None
        self.context.move_pending = False
        position = payload.get("position")
        if position:
            try:
                self.context.position = (int(position[0]), int(position[1]))
            except Exception:
                pass
        self._transition_behavior(BehaviorState.idle)

    def build_runtime_event(self, event_type: EventType, payload: Optional[Dict[str, Any]] = None) -> QueuedEvent:
        self._sequence += 1
        return QueuedEvent(
            priority=DEFAULT_PRIORITY.get(event_type, 0),
            timestamp=time.monotonic(),
            sequence=self._sequence,
            event_type=event_type,
            payload=dict(payload or {}),
        )
