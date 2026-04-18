#!/usr/bin/env python3

"""Agent Py

(a.k.a. Clippy for Linux)

'Cuz FUCK YOU, that's why.

"""
from __future__ import annotations

import argparse
import io
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
import textwrap
from pathlib import Path

from agentpy_parser import AgentCharacter, ACSParser, test_sack
from clippy_state_machine import ClippyStateMachine, EventType
def _acs_palette_to_bytes(color_table):
    palette = bytearray()
    for color in color_table:
        palette.extend((color.red, color.green, color.blue))
    palette.extend(b"\x00" * (256 * 3 - len(palette)))
    return bytes(palette[:256 * 3])

def _acs_palette_to_rgba_table(color_table):
    table = []
    for color in color_table:
        table.append((int(color.red), int(color.green), int(color.blue), 255))
    while len(table) < 256:
        table.append((0, 0, 0, 255))
    return table[:256]

def _require_pillow():
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required for image rendering. Install it with `pip install Pillow`."
        ) from exc
    return Image

class AgentRuntime(object):
    def __init__(self, character_path):
        self.character_path = _resolve_acs_path(character_path)
        start = time.perf_counter()
        self.agent = AgentCharacter(self.character_path)
        self.parse_seconds = time.perf_counter() - start

        self.character = self.agent.data.acscharacterinfo
        self.palette_bytes = _acs_palette_to_bytes(self.character.color_table)
        self.palette_rgba_table = _acs_palette_to_rgba_table(self.character.color_table)
        self.transparent_color_index = self._resolve_transparent_color_index()
        self.render_profile = "generic-rgba-palette"
        self.sprite_cache = {}
        self.animation_cache = {}
        self.state_cache = {}
        self.timeline_cache = {}
        self.balloon_cache = {}
        self._animation_object_cache = {}
        self.audio_cache = {}
        self._image_module = None
        self._sprite_cache_built = False
        self.sprite_cache_seconds = None

        print(f"ACS parse: {self.parse_seconds:.3f}s")

    def _get_image_module(self):
        if self._image_module is None:
            self._image_module = _require_pillow()
        return self._image_module

    def _resolve_transparent_color_index(self):
        transparent_color_index = int(getattr(self.character, "transparent_color_index", -1))
        if 0 <= transparent_color_index < 256:
            return transparent_color_index
        return None

    def _palette_index_to_rgba(self, palette_index):
        index = int(palette_index)
        if 0 <= index < len(self.palette_rgba_table):
            return self.palette_rgba_table[index]
        return (0, 0, 0, 255)

    def _sprite_data_to_rgba_bytes(self, sprite_info):
        pixel_data = bytes(sprite_info.image_data)
        width = int(getattr(sprite_info, "width", 0))
        height = int(getattr(sprite_info, "height", 0))
        if width <= 0 or height <= 0:
            return b""
        expected_stride = ((width + 3) // 4) * 4
        actual_stride = len(pixel_data) // height if height else width
        stride = expected_stride if len(pixel_data) >= expected_stride * height else max(width, actual_stride)
        transparent_index = self.transparent_color_index
        rgba = bytearray(width * height * 4)
        out = 0
        row_end = min(len(pixel_data), stride * height)
        for row_start in range(0, row_end, stride):
            row = pixel_data[row_start:row_start + width]
            for value in row:
                red, green, blue, alpha = self._palette_index_to_rgba(value)
                if transparent_index is not None and int(value) == int(transparent_index):
                    alpha = 0
                rgba[out] = red
                rgba[out + 1] = green
                rgba[out + 2] = blue
                rgba[out + 3] = alpha
                out += 4
        if out < len(rgba):
            rgba[out:] = b"\x00" * (len(rgba) - out)
        return bytes(rgba)

    def _ensure_sprite_cache(self):
        if self._sprite_cache_built:
            return
        start = time.perf_counter()
        image_module = self._get_image_module()
        for index, image_info in enumerate(self.agent.data.acsimageinfo):
            sprite_info = image_info.image_data
            if sprite_info.width <= 0 or sprite_info.height <= 0:
                raise ValueError(f"invalid sprite dimensions at image index {index}")
            # Decode directly to RGBA so we do not depend on agent-specific PIL palette
            # semantics. This keeps sprite colors consistent across Clippy, Merlin, and
            # any future ACS character that uses the same indexed bitmap model.
            sprite = image_module.frombytes(
                "RGBA",
                (sprite_info.width, sprite_info.height),
                self._sprite_data_to_rgba_bytes(sprite_info),
            )
            # ACS bitmaps are stored bottom-up like Windows DIBs.
            sprite = sprite.transpose(image_module.FLIP_TOP_BOTTOM)
            self.sprite_cache[index] = sprite
        self.sprite_cache_seconds = time.perf_counter() - start
        self._sprite_cache_built = True
        print(f"Sprite decode cache build: {self.sprite_cache_seconds:.3f}s")

    def list_animations(self):
        return sorted(self.agent.data.acsanimationinfo, key=lambda item: item.name.casefold())

    def list_states(self):
        return sorted(self.character.stateinfo.values(), key=lambda item: item.name.casefold())

    def find_animation(self, animation_name):
        wanted = animation_name.casefold()
        for animation in self.agent.data.acsanimationinfo:
            if animation.name.casefold() == wanted:
                return animation
        available = [animation.name for animation in self.list_animations()]
        raise ValueError(
            f"animation {animation_name!r} not found. Available animations: "
            + ", ".join(available)
        )

    def _get_animation_object(self, animation_name):
        wanted = str(animation_name).casefold()
        cached = self._animation_object_cache.get(wanted)
        if cached is not None:
            return cached
        animation = self.find_animation(animation_name)
        self._animation_object_cache[wanted] = animation
        return animation

    def find_state(self, state_name):
        wanted = state_name.casefold()
        for state in self.character.stateinfo.values():
            if state.name.casefold() == wanted:
                return state
        available = [state.name for state in self.list_states()]
        raise ValueError(
            f"state {state_name!r} not found. Available states: "
            + ", ".join(available)
        )

    def available_animation_names(self):
        return [animation.name for animation in self.list_animations()]

    def available_state_names(self):
        return [state.name for state in self.list_states()]

    def list_audio_clips(self):
        return list(enumerate(self.agent.data.acsaudioinfo))

    def audio_clips_text(self):
        lines = ["Audio clips:"]
        for index, audio in self.list_audio_clips():
            lines.append(f"- {index} (size: {len(audio.audio_data)} bytes)")
        lines.append(f"Total: {len(self.agent.data.acsaudioinfo)} clips")
        return "\n".join(lines)

    def print_audio_clips(self):
        print(self.audio_clips_text())

    def audio_usage(self):
        usage = {}
        for animation in self.list_animations():
            frames = []
            for index, frame in enumerate(animation.animation_data.frames, start=1):
                audio_index = getattr(frame, "audio_index", 65535)
                if audio_index not in (None, 65535):
                    frames.append((index, int(audio_index)))
            if frames:
                usage[animation.name] = frames
        return usage

    def audio_usage_text(self):
        usage = self.audio_usage()
        lines = ["Audio usage:"]
        if not usage:
            lines.append("(no audio usage found)")
            return "\n".join(lines)
        for animation_name in sorted(usage, key=str.casefold):
            lines.append("")
            lines.append(f"Animation: {animation_name}")
            for frame_index, audio_index in usage[animation_name]:
                lines.append(f"  Frame {frame_index} -> audio {audio_index}")
        return "\n".join(lines)

    def print_audio_usage(self):
        print(self.audio_usage_text())

    def _format_audio_name(self, format_tag):
        format_names = {
            0: "Unknown",
            1: "PCM",
            2: "ADPCM",
            3: "IEEE_FLOAT",
            6: "ALAW",
            7: "MULAW",
            17: "IMA ADPCM",
            65534: "EXTENSIBLE",
        }
        return format_names.get(int(format_tag), f"format {int(format_tag)}")

    def _parse_wav_info(self, audio_data):
        if len(audio_data) < 12 or not audio_data.startswith(b"RIFF") or audio_data[8:12] != b"WAVE":
            return {
                "is_wav": False,
                "format_tag": None,
                "format_name": "unknown",
                "channels": None,
                "sample_rate": None,
                "bits_per_sample": None,
                "byte_rate": None,
                "data_size": len(audio_data),
                "duration_seconds": None,
            }

        offset = 12
        fmt = None
        data_size = None
        while offset + 8 <= len(audio_data):
            chunk_id = audio_data[offset:offset + 4]
            chunk_size = struct.unpack_from("<I", audio_data, offset + 4)[0]
            chunk_data_offset = offset + 8
            chunk_end = chunk_data_offset + chunk_size
            if chunk_end > len(audio_data):
                break
            if chunk_id == b"fmt ":
                fmt_data = audio_data[chunk_data_offset:chunk_end]
                if len(fmt_data) < 16:
                    raise ValueError("audio clip has an invalid fmt chunk")
                format_tag, channels, sample_rate, byte_rate, block_align, bits_per_sample = struct.unpack_from("<HHIIHH", fmt_data, 0)
                fmt = {
                    "format_tag": int(format_tag),
                    "channels": int(channels),
                    "sample_rate": int(sample_rate),
                    "byte_rate": int(byte_rate),
                    "block_align": int(block_align),
                    "bits_per_sample": int(bits_per_sample),
                }
            elif chunk_id == b"data":
                data_size = int(chunk_size)
            offset = chunk_end + (chunk_size % 2)

        if fmt is None:
            raise ValueError("audio clip is missing a fmt chunk")

        duration_seconds = None
        if fmt["byte_rate"] > 0 and data_size is not None:
            duration_seconds = data_size / float(fmt["byte_rate"])

        return {
            "is_wav": True,
            "format_tag": fmt["format_tag"],
            "format_name": self._format_audio_name(fmt["format_tag"]),
            "channels": fmt["channels"],
            "sample_rate": fmt["sample_rate"],
            "bits_per_sample": fmt["bits_per_sample"],
            "byte_rate": fmt["byte_rate"],
            "data_size": data_size,
            "duration_seconds": duration_seconds,
        }

    def audio_info(self):
        info = []
        for index, audio in self.list_audio_clips():
            audio_data = bytes(audio.audio_data)
            parsed = self._parse_wav_info(audio_data)
            parsed["index"] = int(index)
            parsed["size"] = len(audio_data)
            info.append(parsed)
        return info

    def audio_info_text(self):
        clips = self.audio_info()
        lines = ["Audio clip info:"]
        format_counts = {}
        total_duration = 0.0
        for clip in clips:
            if clip["is_wav"]:
                format_label = f"format={clip['format_tag']} ({clip['format_name']})"
                if clip["format_name"] in ("PCM", "ADPCM", "IMA ADPCM", "IEEE_FLOAT", "ALAW", "MULAW"):
                    format_counts[clip["format_name"]] = format_counts.get(clip["format_name"], 0) + 1
                else:
                    format_counts["Unknown"] = format_counts.get("Unknown", 0) + 1
            else:
                format_label = "not RIFF/WAV"
                format_counts["Unknown"] = format_counts.get("Unknown", 0) + 1

            duration = clip["duration_seconds"]
            if duration is not None:
                total_duration += duration
                duration_text = f"{duration:.2f} s"
            else:
                duration_text = "n/a"

            channels_text = "n/a" if clip["channels"] is None else f"{clip['channels']} ch"
            rate_text = "n/a" if clip["sample_rate"] is None else f"{clip['sample_rate']} Hz"
            bits_text = ""
            if clip["bits_per_sample"] is not None and clip["bits_per_sample"] > 0:
                bits_text = f" | {clip['bits_per_sample']}-bit"

            lines.append(
                f"- {clip['index']} | "
                f"{'RIFF/WAV' if clip['is_wav'] else 'RAW'} | "
                f"{format_label} | "
                f"{rate_text} | "
                f"{channels_text}{bits_text} | "
                f"duration: {duration_text} | "
                f"{clip['size']} bytes"
            )

        if clips:
            lines.extend([
                "",
                f"Total audio duration: {total_duration:.2f} s",
                "Format counts:",
            ])
            for name in sorted(format_counts, key=str.casefold):
                lines.append(f"- {name}: {format_counts[name]}")
        else:
            lines.extend(["", "Total audio duration: 0.00 s", "Format counts:", "- Unknown: 0"])
        return "\n".join(lines)

    def print_audio_info(self):
        print(self.audio_info_text())

    def animation_timeline(self, animation_name):
        if not hasattr(self, "timeline_cache") or self.timeline_cache is None:
            self.timeline_cache = {}
        animation = self.find_animation(animation_name)
        cache_key = animation.name.casefold()
        cached = self.timeline_cache.get(cache_key)
        if cached is not None:
            return animation, cached, True

        entries = []
        for frame_index, frame in enumerate(animation.animation_data.frames):
            overlay_types = [
                getattr(overlay, "overlay_type", None) or "-"
                for overlay in (getattr(frame, "mouth_overlays", []) or [])
            ]
            branches = [
                (
                    int(getattr(branch, "jump_to_frame_index", -1)),
                    int(getattr(branch, "probability_percent", 0)),
                )
                for branch in (getattr(frame, "frame_branches", []) or [])
            ]
            entries.append(
                {
                    "frame_index": frame_index,
                    "duration_csecs": int(frame.frame_duration_csecs or 0),
                    "duration_ms": max(1, int(round((frame.frame_duration_csecs or 1) * 10))),
                    "audio_index": int(getattr(frame, "audio_index", 65535)),
                    "overlay_types": overlay_types,
                    "images": list(frame.images),
                    "image_count": len(frame.images),
                    "exit_to_frame_index": int(getattr(frame, "exit_to_frame_index", -1)),
                    "branches": branches,
                    "frame": frame,
                }
            )

        self.timeline_cache[cache_key] = entries
        return animation, entries, False

    def timeline(self, animation_name):
        animation, entries, cached = self.animation_timeline(animation_name)
        frames = animation.animation_data.frames
        total_duration_csecs = 0
        audio_indices = set()
        overlay_types = set()
        has_branches = False

        lines = [
            f"Animation: {animation.name}",
            f"Transition type: {animation.animation_data.transition_type or '-'}",
            f"Return animation: {animation.animation_data.return_animation or '-'}",
            f"Frames: {len(frames)}",
            "",
        ]

        for entry in entries:
            index = entry["frame_index"]
            duration = entry["duration_csecs"]
            total_duration_csecs += duration
            audio_index = entry["audio_index"]
            if audio_index not in (None, 65535):
                audio_indices.add(audio_index)
                audio_text = str(audio_index)
            else:
                audio_text = "-"

            frame_overlay_types = entry["overlay_types"]
            for overlay_type in frame_overlay_types:
                overlay_types.add(str(overlay_type))

            branches = entry["branches"]
            if branches:
                has_branches = True
                branch_parts = [
                    f"{jump_to}@{probability}%"
                    for jump_to, probability in branches
                ]
                branch_text = ", ".join(branch_parts)
            else:
                branch_text = "-"

            exit_to_frame_index = entry["exit_to_frame_index"]
            exit_text = "-" if exit_to_frame_index in (None, -1) else str(exit_to_frame_index)
            overlays_text = ", ".join(frame_overlay_types) if frame_overlay_types else "-"

            lines.append(
                f"Frame {index} | duration={duration} | audio={audio_text} | "
                f"overlays={overlays_text} | images={entry['image_count']} | "
                f"exit={exit_text} | branches={branch_text}"
            )

        lines.extend([
            "",
            f"Total duration: {total_duration_csecs / 100.0:.2f} s",
            "Distinct audio clips: " + (", ".join(str(i) for i in sorted(audio_indices)) if audio_indices else "-"),
            "Distinct overlay types: " + (", ".join(sorted(overlay_types, key=str.casefold)) if overlay_types else "-"),
            f"Has branches: {'yes' if has_branches else 'no'}",
            f"Return animation: {animation.animation_data.return_animation or '-'}",
            f"Transition type: {animation.animation_data.transition_type or '-'}",
        ])
        return "\n".join(lines)

    def print_timeline(self, animation_name):
        print(self.timeline(animation_name))

    def animation_analysis(self, animation_name):
        animation, entries, cached = self.animation_timeline(animation_name)
        frames = animation.animation_data.frames
        frame_count = len(entries)
        total_duration_csecs = sum(entry["duration_csecs"] for entry in entries)
        frames_with_audio = [entry for entry in entries if entry["audio_index"] not in (None, 65535)]
        distinct_audio = sorted({entry["audio_index"] for entry in frames_with_audio})
        distinct_overlays = sorted(
            {
                str(overlay_type)
                for entry in entries
                for overlay_type in entry["overlay_types"]
                if str(overlay_type) not in {"-", ""}
            },
            key=str.casefold,
        )
        branches_exist = any(entry["branches"] for entry in entries)
        exit_frames_exist = any(entry["exit_to_frame_index"] not in (None, -1) for entry in entries)
        return_animation = animation.animation_data.return_animation
        transition_type = animation.animation_data.transition_type
        image_counts = [entry["image_count"] for entry in entries] or [0]
        min_images = min(image_counts)
        max_images = max(image_counts)
        avg_images = sum(image_counts) / float(len(image_counts))
        total_duration_seconds = total_duration_csecs / 100.0

        speaking = bool(frames_with_audio)
        mouth_overlays_present = bool(distinct_overlays)
        looping = bool(return_animation) or transition_type == "use_return_animation"

        if branches_exist:
            classification = "branched"
        elif speaking and mouth_overlays_present:
            classification = "speaking-with-mouth-overlays"
        elif speaking:
            classification = "speaking"
        elif looping:
            classification = "looping"
        else:
            classification = "gesture-only"

        complexity_score = 0
        complexity_score += min(3, frame_count // 8)
        complexity_score += 1 if total_duration_seconds >= 2.0 else 0
        complexity_score += 1 if speaking else 0
        complexity_score += 1 if mouth_overlays_present else 0
        complexity_score += 1 if branches_exist else 0
        complexity_score += 1 if max_images >= 5 else 0
        if complexity_score <= 2:
            complexity = "low"
        elif complexity_score <= 5:
            complexity = "medium"
        else:
            complexity = "high"

        lines = [
            f"Animation analysis: {animation.name}",
            f"Type: {classification}",
            f"Complexity: {complexity}",
            "",
            f"Frames: {frame_count}",
            f"Total duration: {total_duration_seconds:.2f} s",
            f"Frames with audio: {len(frames_with_audio)}",
            "Distinct audio clips: "
            + (f"{len(distinct_audio)} -> [{', '.join(str(i) for i in distinct_audio)}]" if distinct_audio else "0 -> [-]"),
            "Distinct overlays: " + (", ".join(distinct_overlays) if distinct_overlays else "-"),
            f"Branches: {'yes' if branches_exist else 'no'}",
            f"Exit frames: {'yes' if exit_frames_exist else 'no'}",
            f"Return animation: {return_animation or '-'}",
            f"Transition type: {transition_type or '-'}",
            f"Images per frame: min={min_images} max={max_images} avg={avg_images:.1f}",
        ]
        return {
            "animation": animation,
            "cached": cached,
            "text": "\n".join(lines),
        }

    def print_animation_analysis(self, animation_name):
        analysis = self.animation_analysis(animation_name)
        print(analysis["text"])

    def _get_audio_data(self, audio_index):
        audio_index = int(audio_index)
        if audio_index < 0 or audio_index >= len(self.agent.data.acsaudioinfo):
            raise ValueError(f"audio index out of range: {audio_index}")
        return bytes(self.agent.data.acsaudioinfo[audio_index].audio_data)

    def _probe_wav_format_tag(self, audio_data):
        if len(audio_data) < 20 or not audio_data.startswith(b"RIFF") or audio_data[8:12] != b"WAVE":
            raise ValueError("audio clip is not a valid RIFF/WAVE stream")
        offset = 12
        while offset + 8 <= len(audio_data):
            chunk_id = audio_data[offset:offset + 4]
            chunk_size = struct.unpack_from("<I", audio_data, offset + 4)[0]
            chunk_data_offset = offset + 8
            if chunk_id == b"fmt ":
                if chunk_size < 2 or chunk_data_offset + 2 > len(audio_data):
                    raise ValueError("audio clip has an invalid fmt chunk")
                return struct.unpack_from("<H", audio_data, chunk_data_offset)[0]
            offset = chunk_data_offset + chunk_size + (chunk_size % 2)
        raise ValueError("audio clip is missing a fmt chunk")

    def _convert_adpcm_wav_to_pcm_bytes(self, audio_index, audio_data):
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("Audio format not supported (ADPCM). Try installing ffmpeg.")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            input_path = tmpdir / f"audio_{audio_index}.wav"
            output_path = tmpdir / f"audio_{audio_index}_pcm.wav"
            input_path.write_bytes(audio_data)
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(input_path), str(output_path)],
                    check=True,
                    capture_output=True,
                )
            except (FileNotFoundError, subprocess.CalledProcessError) as exc:
                raise RuntimeError("Audio format not supported (ADPCM). Try installing ffmpeg.") from exc
            if not output_path.exists():
                raise RuntimeError("Audio format not supported (ADPCM). Try installing ffmpeg.")
            return output_path.read_bytes()

    def _wave_object_from_pcm_bytes(self, simpleaudio, wav_data):
        if hasattr(simpleaudio.WaveObject, "from_wave_file"):
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(wav_data)
                temp_path = tmp.name
            try:
                return simpleaudio.WaveObject.from_wave_file(temp_path)
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
        with wave.open(io.BytesIO(wav_data), "rb") as wav_file:
            audio_data = wav_file.readframes(wav_file.getnframes())
            return simpleaudio.WaveObject(
                audio_data,
                wav_file.getnchannels(),
                wav_file.getsampwidth(),
                wav_file.getframerate(),
            )

    def _prepare_audio_clip(self, audio_index):
        audio_index = int(audio_index)
        cached = self.audio_cache.get(audio_index)
        if cached is not None:
            return cached
        audio_data = self._get_audio_data(audio_index)
        format_tag = self._probe_wav_format_tag(audio_data)
        if format_tag == 2:
            print("Audio format: ADPCM -> converting to PCM")
            audio_data = self._convert_adpcm_wav_to_pcm_bytes(audio_index, audio_data)
        elif format_tag != 1:
            raise ValueError(f"audio clip {audio_index} uses unsupported WAV format {format_tag}")
        try:
            import simpleaudio
        except ImportError as exc:
            raise RuntimeError("Audio playback requires installing simpleaudio") from exc
        wave_object = self._wave_object_from_pcm_bytes(simpleaudio, audio_data)
        self.audio_cache[audio_index] = wave_object
        return wave_object

    def play_audio_clip(self, audio_index):
        wave_object = self._prepare_audio_clip(audio_index)
        play_object = wave_object.play()
        return play_object

    def extract_audio_clip(self, audio_index, output_path=None):
        audio_index = int(audio_index)
        audio_data = self._get_audio_data(audio_index)
        if output_path is None:
            suffix = ".wav" if audio_data.startswith(b"RIFF") else ".bin"
            output = Path.cwd() / f"audio_{audio_index}{suffix}"
        else:
            output = Path(output_path)
        output.write_bytes(audio_data)
        return output

    def info_text(self):
        character = self.character
        data = self.agent.data
        localized = character.localizedinfo
        states = self.list_states()
        animation_names = [animation.name for animation in self.list_animations()]
        assigned_animation_names = {
            str(animation_name).casefold()
            for state in states
            for animation_name in state.animations
        }
        unassigned_animations = [
            name for name in animation_names
            if name.casefold() not in assigned_animation_names
        ]

        lines = [
            "Character info",
            "--------------",
            f"File: {self.character_path}",
            f"Signature: 0x{data.signature:08x}",
            f"GUID: {character.guid}",
            f"Version: {character.major_version}.{character.minor_version}",
            f"Canvas size: {character.width} x {character.height}",
            f"Transparent color index: {character.transparent_color_index}",
            f"Palette entries: {len(character.color_table)}",
            f"Render profile: {self.render_profile}",
            f"Voice: {'yes' if character.voiceinfo else 'no'}",
            f"Balloon: {'yes' if character.ballooninfo else 'no'}",
            "",
            f"States: {len(states)}",
            f"Animations: {len(animation_names)}",
            f"Images: {len(data.acsimageinfo)}",
            f"Audio clips: {len(data.acsaudioinfo)}",
            f"Unassigned animations: {len(unassigned_animations)}",
        ]

        if localized:
            lines.extend(["", "Localized info"])
            for lang_id, entry in sorted(localized.items(), key=lambda item: item[0]):
                lines.append(
                    f"- {lang_id}: {entry.name or '<no name>'} | {entry.desc or '<no description>'}"
                )

        if character.voiceinfo:
            voice = character.voiceinfo
            lines.extend([
                "",
                "Voice info",
                f"- speed: {voice.speed}",
                f"- pitch: {voice.pitch}",
                f"- lang_id: {voice.lang_id}",
                f"- gender: {voice.gender}",
                f"- age: {voice.age}",
                f"- style: {voice.style}",
            ])

        if character.ballooninfo:
            balloon = character.ballooninfo
            lines.extend([
                "",
                "Balloon info",
                f"- num_lines: {balloon.num_lines}",
                f"- chars_per_line: {balloon.chars_per_line}",
                f"- font_name: {balloon.font_name}",
                f"- font_height: {balloon.font_height}",
                f"- italic_flag: {balloon.italic_flag}",
            ])

        lines.extend(["", "State coverage:"])
        for state in states:
            lines.append(f"- {state.name}: {len(state.animations)} animations")

        return "\n".join(lines)

    def print_info(self):
        print(self.info_text())

    def balloon_info_text(self):
        balloon = self.character.ballooninfo
        if not balloon:
            return "\n".join([
                "Balloon info",
                "-------------",
                "Balloon: no",
            ])
        practical_width = None
        practical_chars = None
        effective_char_width = None
        try:
            layout = self._balloon_layout("012345678901234567890123456789", base_size=(self.character.width, self.character.height))
            practical_width = layout.get("practical_width_px")
            practical_chars = layout.get("practical_chars_per_line")
            if practical_width is not None and practical_chars:
                effective_char_width = practical_width / max(1, practical_chars)
        except Exception:
            pass
        return "\n".join([
            "Balloon info",
            "-------------",
            "Balloon: yes",
            f"num_lines (ACS metadata): {balloon.num_lines}",
            f"chars_per_line (ACS metadata): {balloon.chars_per_line}",
            f"practical_width_px (measured): {practical_width if practical_width is not None else '-'}",
            f"practical_chars_per_line (measured): {practical_chars if practical_chars is not None else '-'}",
            f"effective_char_width_px (measured): {effective_char_width if effective_char_width is not None else '-'}",
            f"fgcolor: {self._rgbquad_to_tuple(balloon.fgcolor)}",
            f"bgcolor: {self._rgbquad_to_tuple(balloon.bgcolor)}",
            f"border_color: {self._rgbquad_to_tuple(balloon.border_color)}",
            f"font_name: {balloon.font_name or '-'}",
            f"font_height: {balloon.font_height}",
            f"font_weight: {balloon.font_weight}",
            f"italic_flag: {balloon.italic_flag}",
            f"unknown: {balloon.unknown}",
        ])

    def print_balloon_info(self):
        print(self.balloon_info_text())

    def _rgbquad_to_tuple(self, color):
        if color is None:
            return (0, 0, 0, 255)
        if hasattr(color, "red") and hasattr(color, "green") and hasattr(color, "blue"):
            red = int(color.red)
            green = int(color.green)
            blue = int(color.blue)
            return (red, green, blue, 255)
        red = int(color[0] if len(color) > 0 else 0)
        green = int(color[1] if len(color) > 1 else 0)
        blue = int(color[2] if len(color) > 2 else 0)
        return (red, green, blue, 255)

    def _load_balloon_font(self):
        from PIL import ImageFont
        balloon = self.character.ballooninfo
        if not balloon:
            return ImageFont.load_default()
        font_size = max(10, abs(int(getattr(balloon, "font_height", 14) or 14)))
        font_name = str(getattr(balloon, "font_name", "") or "").strip()
        font_candidates = []
        if font_name:
            font_candidates.append(font_name)
            if not font_name.lower().endswith(".ttf"):
                font_candidates.append(font_name + ".ttf")
        for candidate in font_candidates:
            try:
                return ImageFont.truetype(candidate, font_size)
            except OSError:
                continue
        return ImageFont.load_default()

    def _measure_text_bbox(self, draw, text, font, spacing=0):
        if not text:
            try:
                return draw.textbbox((0, 0), " ", font=font)
            except AttributeError:
                size = draw.textsize(" ", font=font)
                return (0, 0, size[0], size[1])
        try:
            return draw.multiline_textbbox((0, 0), text, font=font, spacing=spacing)
        except AttributeError:
            size = draw.multiline_textsize(text, font=font, spacing=spacing)
            return (0, 0, size[0], size[1])

    def _measure_line_width(self, draw, text, font):
        bbox = self._measure_text_bbox(draw, text, font)
        return bbox[2] - bbox[0]

    def _wrap_balloon_text(self, text, draw, font, max_width_px, max_lines):
        def measure(line):
            return self._measure_line_width(draw, line, font)

        def break_word(word):
            if not word:
                return [""]
            pieces = []
            chunk = ""
            for char in word:
                candidate = chunk + char
                if chunk and measure(candidate) > max_width_px:
                    pieces.append(chunk)
                    chunk = char
                else:
                    chunk = candidate
            if chunk:
                pieces.append(chunk)
            return pieces or [word]

        raw_lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
        wrapped_lines = []
        for raw_line in raw_lines:
            words = raw_line.split()
            if not words:
                wrapped_lines.append("")
                continue
            current = words[0]
            for word in words[1:]:
                candidate = current + " " + word
                if measure(candidate) <= max_width_px:
                    current = candidate
                    continue
                if current:
                    wrapped_lines.append(current)
                if measure(word) <= max_width_px:
                    current = word
                else:
                    split_words = break_word(word)
                    wrapped_lines.extend(split_words[:-1])
                    current = split_words[-1]
            wrapped_lines.append(current)

        cleaned_lines = [line.rstrip() for line in wrapped_lines if line is not None]
        if len(cleaned_lines) > max_lines:
            cleaned_lines = cleaned_lines[:max_lines]
            cleaned_lines[-1] = cleaned_lines[-1].rstrip()
            if len(cleaned_lines[-1]) >= 3:
                cleaned_lines[-1] = cleaned_lines[-1][:-3].rstrip() + "..."
            else:
                cleaned_lines[-1] = "..."
        return cleaned_lines or [""]

    def _balloon_layout(self, text, base_size=None):
        from PIL import Image, ImageDraw
        base_width, base_height = base_size or (self.character.width, self.character.height)
        balloon = self.character.ballooninfo
        overlay = Image.new("RGBA", (base_width, base_height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        font = self._load_balloon_font()
        font_height = abs(int(getattr(balloon, "font_height", 14) or 14)) if balloon else 14
        spacing = max(1, int(round(font_height * 0.15)))
        balloon_lines = max(1, int(getattr(balloon, "num_lines", 3) or 3)) if balloon else 3
        chars_per_line = max(1, int(getattr(balloon, "chars_per_line", 24) or 24)) if balloon else 24
        try:
            sample_text = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            sample_width = self._measure_line_width(draw, sample_text, font)
            average_char_width = max(6, int(round(sample_width / max(1, len(sample_text)))))
        except Exception:
            average_char_width = 8
        inner_margin_x = 14
        inner_margin_y = 10
        tail_height = max(10, int(round(font_height * 0.9)))
        practical_width_px = max(48, base_width - 20 - inner_margin_x * 2)
        practical_chars_per_line = max(1, int(practical_width_px / max(1, average_char_width)))
        soft_width_px = chars_per_line * average_char_width
        max_width_px = min(
            max(96, int(base_width * 0.42)),
            max(96, soft_width_px + average_char_width),
            practical_width_px,
        )
        lines = self._wrap_balloon_text(text, draw, font, max_width_px, balloon_lines)
        text_block = "\n".join(lines)
        bbox = self._measure_text_bbox(draw, text_block, font, spacing=spacing)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        padding_x = max(10, min(16, int(round(text_height * 0.12))))
        padding_y = max(6, min(12, int(round(text_height * 0.10))))
        bubble_width = min(base_width - 20, max(text_width + padding_x * 2, 100))
        bubble_height = min(base_height - 20, max(text_height + padding_y * 2 + tail_height, 42))
        bubble_x = max(8, base_width - bubble_width - 12)
        bubble_y = max(2, min(6, int(round(font_height * 0.12))))
        bubble_right = min(base_width - 8, bubble_x + bubble_width)
        bubble_bottom = min(base_height - 8, bubble_y + bubble_height)
        bubble_width = bubble_right - bubble_x
        bubble_height = bubble_bottom - bubble_y
        if balloon:
            bg = self._rgbquad_to_tuple(balloon.bgcolor)
            fg = self._rgbquad_to_tuple(balloon.fgcolor)
            border = self._rgbquad_to_tuple(balloon.border_color)
        else:
            bg = (255, 255, 192, 200)
            fg = (0, 0, 0, 255)
            border = (0, 0, 0, 255)
        bg = bg[:3] + (200,)
        border = border[:3] + (255,)
        text_x = bubble_x + padding_x
        text_y = bubble_y + padding_y
        return {
            "base_width": base_width,
            "base_height": base_height,
            "overlay": overlay,
            "draw": draw,
            "font": font,
            "font_height": font_height,
            "spacing": spacing,
            "practical_width_px": practical_width_px,
            "practical_chars_per_line": practical_chars_per_line,
            "lines": lines,
            "text_block": text_block,
            "text_x": text_x,
            "text_y": text_y,
            "fg": fg,
            "bg": bg,
            "border": border,
            "bubble_x": bubble_x,
            "bubble_y": bubble_y,
            "bubble_right": bubble_right,
            "bubble_bottom": bubble_bottom,
            "bubble_width": bubble_width,
            "bubble_height": bubble_height,
            "tail_height": tail_height,
        }

    def balloon_overlay(self, text, base_size=None):
        cache_key = (str(text), *(base_size or (self.character.width, self.character.height)))
        cached = self.balloon_cache.get(cache_key)
        if cached is not None:
            return cached.copy()
        layout = self._balloon_layout(text, base_size)
        overlay = layout["overlay"]
        draw = layout["draw"]
        draw.rounded_rectangle(
            [layout["bubble_x"], layout["bubble_y"], layout["bubble_right"], layout["bubble_bottom"]],
            radius=10,
            fill=layout["bg"],
            outline=layout["border"],
            width=2,
        )
        tail_top = max(layout["bubble_bottom"] - 1, 0)
        tail_x_mid = max(layout["bubble_x"] + 20, min(layout["bubble_right"] - 20, layout["bubble_x"] + max(24, layout["bubble_width"] // 5)))
        tail = [
            (tail_x_mid - 8, tail_top),
            (tail_x_mid + 8, tail_top),
            (tail_x_mid, min(layout["base_height"] - 1, tail_top + layout["tail_height"])),
        ]
        draw.polygon(tail, fill=layout["bg"], outline=layout["border"])
        draw.multiline_text((layout["text_x"], layout["text_y"]), layout["text_block"], fill=layout["fg"], font=layout["font"], spacing=layout["spacing"])
        if BALLOON_DEBUG:
            print(f"Balloon debug: width={layout['bubble_width']} height={layout['bubble_height']} lines={len(layout['lines'])}")
        self.balloon_cache[cache_key] = overlay
        return overlay.copy()

    def compose_balloon_frame(self, frame, text):
        from PIL import Image
        if not text:
            return frame.copy()
        base = frame.convert("RGBA")
        layout = self._balloon_layout(text, base.size)
        bubble_height = layout["bubble_height"]
        gap = max(8, min(16, int(round(layout["font_height"] * 0.6))))
        sprite_y = bubble_height + gap
        canvas_height = sprite_y + base.height
        canvas = Image.new("RGBA", (base.width, canvas_height), (0, 0, 0, 0))
        overlay = self.balloon_overlay(text, base.size)
        canvas.paste(overlay, (0, 0), overlay)
        canvas.paste(base, (0, sprite_y), base)
        return canvas

    def _normalize_mouth_index(self, mouth):
        mouth_names = {
            0: "mouth_closed",
            1: "mouth_wide_open_1",
            2: "mouth_wide_open_2",
            3: "mouth_wide_open_3",
            4: "mouth_wide_open_4",
            5: "mouth_medium",
            6: "mouth_narrow",
        }
        name_to_index = {name: index for index, name in mouth_names.items()}
        aliases = {
            "closed": 0,
            "neutral": 0,
            "rest": 0,
            "silence": 0,
            "open": 4,
            "wide_open": 4,
            "mouth_open": 4,
            "mouth_wide_open": 4,
            "medium": 5,
            "semi_open": 5,
            "narrow": 6,
        }
        if mouth is None:
            return None
        if isinstance(mouth, int):
            if 0 <= mouth <= 6:
                return mouth
            return None
        if hasattr(mouth, "mouth"):
            mouth = getattr(mouth, "mouth")
        elif hasattr(mouth, "name"):
            mouth = getattr(mouth, "name")
        if mouth is None:
            return None
        if isinstance(mouth, int):
            return mouth if 0 <= mouth <= 6 else None
        text = str(mouth).strip().casefold().replace(" ", "_").replace("-", "_")
        if text in aliases:
            return aliases[text]
        if text in name_to_index:
            return name_to_index[text]
        if text.startswith("mouth_"):
            suffix = text.removeprefix("mouth_")
            if suffix.isdigit():
                idx = int(suffix)
                if 0 <= idx <= 6:
                    return idx
        return None

    def _mouth_name_for_index(self, mouth_index):
        names = {
            0: "mouth_closed",
            1: "mouth_wide_open_1",
            2: "mouth_wide_open_2",
            3: "mouth_wide_open_3",
            4: "mouth_wide_open_4",
            5: "mouth_medium",
            6: "mouth_narrow",
        }
        return names.get(int(mouth_index), "mouth_closed")

    def _find_mouth_overlay_source_frame(self, animation_name, preferred_frame_index=None):
        if animation_name is None:
            return None, None, []
        try:
            animation = self._get_animation_object(animation_name)
            frames = list(getattr(animation.animation_data, "frames", []) or [])
        except Exception:
            return None, None, []
        preferred = None
        if preferred_frame_index is not None and 0 <= int(preferred_frame_index) < len(frames):
            frame = frames[int(preferred_frame_index)]
            overlays = list(getattr(frame, "mouth_overlays", []) or [])
            if overlays:
                return int(preferred_frame_index), frame, overlays
        best = None
        for idx, frame in enumerate(frames):
            overlays = list(getattr(frame, "mouth_overlays", []) or [])
            if not overlays:
                continue
            overlay_indices = {
                self._normalize_mouth_index(getattr(overlay, "overlay_type", None))
                for overlay in overlays
            }
            overlay_indices.discard(None)
            has_full_vocabulary = 1 if len(overlay_indices) >= 7 else 0
            # Generic mouth-bank heuristic: prefer the frame that actually carries
            # the richest reusable mouth set, regardless of whether an ACS author
            # happened to place it on a traditionally "special" frame.
            score = (has_full_vocabulary, len(overlays), -int(idx))
            if best is None or score > best[0]:
                best = (score, idx, frame, overlays)
        if best is None:
            return None, None, []
        _, idx, frame, overlays = best
        return idx, frame, overlays

    def _compose_dynamic_mouth_from_acs(self, base, animation_name, frame_index, mouth_index):
        if animation_name is None or frame_index is None:
            return None
        try:
            animation = self._get_animation_object(animation_name)
            frames = list(getattr(animation.animation_data, "frames", []) or [])
            if frame_index < 0 or frame_index >= len(frames):
                return None
            frame = frames[frame_index]
            overlays = list(getattr(frame, "mouth_overlays", []) or [])
            if not overlays:
                return None
            target_name = self._mouth_name_for_index(mouth_index)
            target_index = self._normalize_mouth_index(target_name)
            overlay_by_name = {}
            available = []
            for overlay in overlays:
                overlay_type = str(getattr(overlay, "overlay_type", "") or "").strip().casefold()
                if not overlay_type:
                    continue
                overlay_by_name[overlay_type] = overlay
                overlay_index = self._normalize_mouth_index(overlay_type)
                if overlay_index is not None:
                    available.append((abs(int(overlay_index) - int(target_index if target_index is not None else 0)), overlay_index, overlay))
            exact = overlay_by_name.get(str(target_name).casefold())
            chosen = exact
            if chosen is None and available:
                available.sort(key=lambda item: (item[0], item[1]))
                chosen = available[0][2]
            if chosen is None and overlay_by_name:
                for preferred in ("mouth_medium", "mouth_closed"):
                    chosen = overlay_by_name.get(preferred)
                    if chosen is not None:
                        break
            if chosen is None:
                return None
            image_index = int(getattr(chosen, "image_index", -1))
            if image_index < 0:
                return None
            mouth_sprite = self._sprite_rgba(image_index)
            canvas = base.convert("RGBA")
            x_offset = int(getattr(chosen, "x_offset", 0))
            y_offset = int(getattr(chosen, "y_offset", 0))
            canvas.paste(mouth_sprite, (x_offset, y_offset), mouth_sprite)
            return canvas
        except Exception:
            return None

    def dump_mouth_overlay_geometry(self, animation_name, frame_index=12):
        animation = self.find_animation(animation_name)
        frames = list(getattr(animation.animation_data, "frames", []) or [])
        if frame_index < 0 or frame_index >= len(frames):
            raise ValueError(f"frame index out of range: {frame_index}")
        frame = frames[frame_index]
        overlays = list(getattr(frame, "mouth_overlays", []) or [])
        branches = [
            (
                int(getattr(branch, "jump_to_frame_index", -1)),
                int(getattr(branch, "probability_percent", 0)),
            )
            for branch in (getattr(frame, "frame_branches", []) or [])
        ]
        region_lines = []
        for overlay_index, overlay in enumerate(overlays):
            region = getattr(overlay, "region_data", None)
            bounds = getattr(region, "bounds", None)
            rects = list(getattr(region, "rects", []) or []) if region is not None else []
            region_lines.append(
                "  "
                + f"[{overlay_index}] type={getattr(overlay, 'overlay_type', None) or '-'} "
                + f"image={getattr(overlay, 'image_index', -1)} "
                + f"replace_top={bool(getattr(overlay, 'replace_top_image_of_frame', False))} "
                + f"region_flag={bool(getattr(overlay, 'region_data_flag', False))} "
                + f"offset=({int(getattr(overlay, 'x_offset', 0))},{int(getattr(overlay, 'y_offset', 0))}) "
                + f"size=({int(getattr(overlay, 'width', 0))}x{int(getattr(overlay, 'height', 0))}) "
                + f"unknown={int(getattr(overlay, 'unknown', -1))}"
            )
            if region is not None:
                region_lines.append(
                    "    "
                    + f"region bounds={getattr(bounds, 'upper_left', None)}->{getattr(bounds, 'lower_right', None)} "
                    + f"rects={len(rects)} buffer_size={getattr(region, 'buffer_size', None)}"
                )
        source_index, _, source_overlays = self._find_mouth_overlay_source_frame(animation_name, preferred_frame_index=frame_index)
        lines = [
            f"Animation: {animation.name}",
            f"Requested frame: {frame_index}",
            f"Selected mouth source frame: {source_index if source_index is not None else '-'}",
            f"Audio index: {int(getattr(frame, 'audio_index', 65535))}",
            f"Duration (csecs): {int(getattr(frame, 'frame_duration_csecs', 0))}",
            f"Exit-to frame: {int(getattr(frame, 'exit_to_frame_index', -1))}",
            "Branches: " + (", ".join(f"{jump}@{prob}%" for jump, prob in branches) if branches else "-"),
            f"Overlay count: {len(overlays)}",
        ]
        if source_index is not None and source_index != frame_index:
            lines.append(f"Note: frame {frame_index} has no mouth overlays; using frame {source_index} as mouth source")
        lines.extend(region_lines if region_lines else ["  -"])
        if source_overlays and source_index != frame_index:
            lines.append("")
            lines.append(f"Source frame {source_index} overlay types: " + ", ".join(
                str(getattr(overlay, "overlay_type", "-")) for overlay in source_overlays
            ))
        return "\n".join(lines)

    def export_mouth_overlay_variants(self, animation_name, frame_index=12, output_dir=None):
        animation = self.find_animation(animation_name)
        source_index, source_frame, source_overlays = self._find_mouth_overlay_source_frame(animation_name, preferred_frame_index=frame_index)
        if source_frame is None:
            raise ValueError(f"no mouth overlay source frame found for {animation_name!r}")
        if output_dir is None:
            output_dir = Path(tempfile.mkdtemp(prefix=f"agentpy_mouth_{animation.name}_"))
        else:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
        base = self._compose_frame(source_frame)
        results = []
        for mouth_index in range(7):
            composed = self._compose_dynamic_mouth_from_acs(base, animation.name, source_index, mouth_index)
            if composed is None:
                composed = self._compose_dynamic_mouth_fallback(base, mouth_index)
            mouth_name = self._mouth_name_for_index(mouth_index)
            output_path = output_dir / f"{animation.name}_frame{source_index}_{mouth_name}.png"
            composed.save(output_path)
            results.append((mouth_index, mouth_name, output_path))
        return source_index, output_dir, results

    def _sprite_stride_info(self, sprite_info):
        width = int(getattr(sprite_info, "width", 0))
        height = int(getattr(sprite_info, "height", 0))
        pixel_data = bytes(getattr(sprite_info, "image_data", b""))
        expected_stride = ((width + 3) // 4) * 4 if width > 0 else 0
        actual_stride = (len(pixel_data) // height) if height > 0 else 0
        used_stride = expected_stride if expected_stride and len(pixel_data) >= expected_stride * max(1, height) else max(width, actual_stride)
        padding_per_row = max(0, used_stride - width)
        return {
            "width": width,
            "height": height,
            "image_bytes": len(pixel_data),
            "expected_stride": expected_stride,
            "actual_stride": actual_stride,
            "used_stride": used_stride,
            "padding_per_row": padding_per_row,
            "explicit_rgba_reconstruction": True,
        }

    def dump_render_profile(self, animation_name=None, frame_index=None):
        lines = [
            "Render profile",
            "--------------",
            f"File: {self.character_path}",
            f"Render profile: {self.render_profile}",
            f"Explicit RGBA reconstruction: yes",
            f"Palette size: {len(self.palette_rgba_table)}",
            f"Transparent color index: {self.transparent_color_index if self.transparent_color_index is not None else '-'}",
            f"Canvas size: {self.character.width} x {self.character.height}",
        ]
        if animation_name is None:
            return "\n".join(lines)
        animation = self.find_animation(animation_name)
        frames = list(getattr(animation.animation_data, "frames", []) or [])
        if not frames:
            lines.append(f"Animation: {animation.name}")
            lines.append("Frame: -")
            return "\n".join(lines)
        if frame_index is None:
            frame_index = 0
        frame_index = int(frame_index)
        if frame_index < 0 or frame_index >= len(frames):
            raise ValueError(f"frame index out of range: {frame_index}")
        frame = frames[frame_index]
        frame_images = list(getattr(frame, "images", []) or [])
        source_index, source_frame, source_overlays = self._find_mouth_overlay_source_frame(animation_name, preferred_frame_index=frame_index)
        lines.extend([
            f"Animation: {animation.name}",
            f"Requested frame: {frame_index}",
            f"Frame image count: {len(frame_images)}",
            f"Frame audio index: {int(getattr(frame, 'audio_index', 65535))}",
            f"Frame duration (csecs): {int(getattr(frame, 'frame_duration_csecs', 0))}",
            f"Frame exit-to index: {int(getattr(frame, 'exit_to_frame_index', -1))}",
            f"Frame branch count: {len(getattr(frame, 'frame_branches', []) or [])}",
            f"Frame mouth overlay count: {len(getattr(frame, 'mouth_overlays', []) or [])}",
            f"Mouth-bank source frame: {source_index if source_index is not None else '-'}",
            f"Using mouth-bank source: {'yes' if source_index is not None else 'no'}",
        ])
        if source_frame is not None and source_index is not None:
            source_overlays_count = len(source_overlays)
            lines.append(f"Source mouth overlay count: {source_overlays_count}")
        if frame_images:
            sprite_index = int(getattr(frame_images[0], "image_index", -1))
            if 0 <= sprite_index < len(self.agent.data.acsimageinfo):
                sprite_info = self.agent.data.acsimageinfo[sprite_index].image_data
                stride_info = self._sprite_stride_info(sprite_info)
                lines.extend([
                    f"Primary image index: {sprite_index}",
                    f"Image size: {stride_info['width']} x {stride_info['height']}",
                    f"Image bytes: {stride_info['image_bytes']}",
                    f"Row stride (expected): {stride_info['expected_stride']}",
                    f"Row stride (actual): {stride_info['actual_stride']}",
                    f"Row stride (used): {stride_info['used_stride']}",
                    f"Row padding per line: {stride_info['padding_per_row']}",
                    f"Explicit RGBA reconstruction: {'yes' if stride_info['explicit_rgba_reconstruction'] else 'no'}",
                ])
            else:
                lines.append(f"Primary image index: {sprite_index}")
                lines.append("Image size: -")
                lines.append("Row stride (expected): -")
                lines.append("Row stride (actual): -")
                lines.append("Row stride (used): -")
                lines.append("Row padding per line: -")
                lines.append("Explicit RGBA reconstruction: yes")
        else:
            lines.append("Primary image index: -")
            lines.append("Image size: -")
            lines.append("Row stride (expected): -")
            lines.append("Row stride (actual): -")
            lines.append("Row stride (used): -")
            lines.append("Row padding per line: -")
            lines.append("Explicit RGBA reconstruction: yes")
        return "\n".join(lines)

    def _compose_dynamic_mouth_fallback(self, base, mouth_index):
        from PIL import Image, ImageDraw
        if mouth_index is None:
            return base.copy()
        width, height = base.size
        if width <= 0 or height <= 0:
            return base.copy()

        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        center_x = int(round(width * 0.50))
        center_y = int(round(height * 0.66))
        mouth_width = max(8, int(round(width * 0.12)))
        mouth_height = max(2, int(round(height * 0.035)))
        mouth_h = mouth_height
        if mouth_index == 0:
            mouth_h = max(1, mouth_height // 3)
        elif mouth_index == 1:
            mouth_h = max(3, int(round(height * 0.025)))
        elif mouth_index == 2:
            mouth_h = max(4, int(round(height * 0.035)))
        elif mouth_index == 3:
            mouth_h = max(5, int(round(height * 0.045)))
        elif mouth_index == 4:
            mouth_h = max(6, int(round(height * 0.055)))
        elif mouth_index == 5:
            mouth_h = max(4, int(round(height * 0.04)))
            mouth_width = max(mouth_width, int(round(width * 0.15)))
        elif mouth_index == 6:
            mouth_h = max(3, int(round(height * 0.028)))
            mouth_width = max(mouth_width, int(round(width * 0.09)))

        left = center_x - mouth_width // 2
        right = center_x + mouth_width // 2
        top = center_y - mouth_h // 2
        bottom = center_y + mouth_h // 2
        border = (25, 25, 25, 220)
        fill = (35, 35, 35, 170)

        if mouth_index == 0:
            draw.line((left, center_y, right, center_y), fill=border, width=max(1, mouth_h))
        elif mouth_index == 6:
            draw.rounded_rectangle([left, top, right, bottom], radius=max(1, mouth_h), fill=fill, outline=border, width=1)
        else:
            draw.ellipse([left, top, right, bottom], fill=fill, outline=border, width=1)
            if mouth_index >= 3:
                inner_top = top + max(1, mouth_h // 4)
                inner_bottom = bottom - max(1, mouth_h // 4)
                inner_left = left + max(1, mouth_width // 5)
                inner_right = right - max(1, mouth_width // 5)
                if inner_right > inner_left and inner_bottom > inner_top:
                    draw.ellipse([inner_left, inner_top, inner_right, inner_bottom], fill=(0, 0, 0, 40))

        return Image.alpha_composite(base.convert("RGBA"), overlay)

    def compose_dynamic_mouth_frame(self, frame, animation_name=None, frame_index=None, mouth=None):
        base = frame.convert("RGBA")
        mouth_index = self._normalize_mouth_index(mouth)
        if mouth_index is None:
            return base.copy()
        composed = self._compose_dynamic_mouth_from_acs(base, animation_name, frame_index, mouth_index)
        if composed is not None:
            return composed
        if animation_name is not None:
            source_index, _, _ = self._find_mouth_overlay_source_frame(animation_name, preferred_frame_index=frame_index)
            if source_index is not None and source_index != frame_index:
                composed = self._compose_dynamic_mouth_from_acs(base, animation_name, source_index, mouth_index)
                if composed is not None:
                    return composed
        return self._compose_dynamic_mouth_fallback(base, mouth_index)

    def show_balloon(self, text, overlay=False, x=None, y=None):
        player = AnimationPlayer(self)
        base_frame = self.first_image()
        try:
            if overlay:
                player.set_overlay(True, x=x, y=y)
            player.play("Balloon", [(base_frame, 100)], fps=1.0, with_audio=False, balloon_text=text)
            player.wait_closed()
        finally:
            player.close()

    def _sprite_rgba(self, image_index):
        self._ensure_sprite_cache()
        image_index = int(image_index)
        if image_index < 0 or image_index >= len(self.agent.data.acsimageinfo):
            raise ValueError(f"image index out of range: {image_index}")
        return self.sprite_cache[image_index]

    def _compose_frame(self, frame):
        self._ensure_sprite_cache()
        image_module = self._get_image_module()
        canvas = image_module.new(
            "RGBA",
            (int(self.character.width), int(self.character.height)),
            (0, 0, 0, 0),
        )
        for frame_image in frame.images:
            sprite = self._sprite_rgba(frame_image.image_index)
            canvas.paste(sprite, (int(frame_image.x_offset), int(frame_image.y_offset)), sprite)
        return canvas

    def render_animation(self, animation_name):
        animation = self.find_animation(animation_name)
        cache_key = animation.name.casefold()
        cached = self.animation_cache.get(cache_key)
        if cached is not None:
            return animation, cached, True

        start = time.perf_counter()
        rendered_frames = []
        for frame in animation.animation_data.frames:
                rendered_frames.append(
                    (
                        self._compose_frame(frame),
                        max(1, int(round((frame.frame_duration_csecs or 1) * 10))),
                        getattr(frame, "audio_index", 65535),
                    )
            )
        elapsed = time.perf_counter() - start
        self.animation_cache[cache_key] = rendered_frames
        print(f"Animation {animation.name!r} composed: {elapsed:.3f}s")
        return animation, rendered_frames, False

    def render_state(self, state_name):
        state = self.find_state(state_name)
        cache_key = state.name.casefold()
        cached = self.state_cache.get(cache_key)
        if cached is not None:
            return state, cached, True

        start = time.perf_counter()
        rendered_frames = []
        for animation_name in state.animations:
            _, animation_frames, _ = self.render_animation(str(animation_name))
            rendered_frames.extend(animation_frames)
        elapsed = time.perf_counter() - start
        self.state_cache[cache_key] = rendered_frames
        print(f"State {state.name!r} composed: {elapsed:.3f}s")
        return state, rendered_frames, False

    def first_image(self):
        if not self.agent.data.acsimageinfo:
            raise ValueError("character file does not contain any image")
        self._ensure_sprite_cache()
        return self._sprite_rgba(0)

    def show_first_image(self):
        image = self.first_image()
        output_path = Path.cwd() / "clippy_first_image.png"
        image.save(output_path, format="PNG")
        print(
            f"First image: {self.character.width}x{self.character.height}, "
            f"compressed={self.agent.data.acsimageinfo[0].image_data.image_compressed}, saved={output_path}"
        )
        image.show()
        return output_path

    def show_animation(self, animation_name, fps=10.0, with_audio=False, say_text=None, overlay=False, x=None, y=None):
        if fps <= 0:
            raise ValueError("fps must be greater than 0")
        animation, rendered_frames, cached = self.render_animation(animation_name)
        if not rendered_frames:
            raise ValueError(f"animation {animation_name!r} contains no frames")
        player = AnimationPlayer(self)
        try:
            if overlay:
                player.set_overlay(True, x=x, y=y)
            player.play(animation.name, rendered_frames, fps=fps, with_audio=with_audio, balloon_text=say_text)
            if cached:
                print(f"Animation {animation.name!r} replayed from cache: immediate")
            player.wait_closed()
        finally:
            player.close()

    def save_animation_gif(self, animation_name, fps=10.0, output_path=None):
        if fps <= 0:
            raise ValueError("fps must be greater than 0")
        animation, rendered_frames, cached = self.render_animation(animation_name)
        if not rendered_frames:
            raise ValueError(f"animation {animation_name!r} contains no frames")
        if cached:
            print(f"Animation {animation.name!r} replayed from cache: immediate")

        output = Path(output_path) if output_path else Path.cwd() / _safe_filename(animation.name, ".gif")
        frames = [frame.copy() for frame, _duration_ms in rendered_frames]
        frame_duration_ms = max(1, int(round(1000.0 / fps)))
        frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=frame_duration_ms,
            loop=0,
            disposal=2,
        )
        return output

    def show_state(self, state_name, fps=10.0, with_audio=False, say_text=None, overlay=False, x=None, y=None):
        if fps <= 0:
            raise ValueError("fps must be greater than 0")
        state, rendered_frames, cached = self.render_state(state_name)
        if not rendered_frames:
            raise ValueError(f"state {state_name!r} contains no frames")
        player = AnimationPlayer(self)
        try:
            if overlay:
                player.set_overlay(True, x=x, y=y)
            player.play(state.name, rendered_frames, fps=fps, with_audio=with_audio, balloon_text=say_text)
            if cached:
                print(f"State {state.name!r} replayed from cache: immediate")
            player.wait_closed()
        finally:
            player.close()

    def show_synced_animation(self, animation_name, fps=10.0, say_text=None, overlay=False, x=None, y=None):
        if fps <= 0:
            raise ValueError("fps must be greater than 0")
        animation, rendered_frames, cached = self.render_animation(animation_name)
        if not rendered_frames:
            raise ValueError(f"animation {animation_name!r} contains no frames")
        _, timeline_entries, timeline_cached = self.animation_timeline(animation_name)
        player = AnimationPlayer(self)
        try:
            if overlay:
                player.set_overlay(True, x=x, y=y)
            player.play(
                animation.name,
                rendered_frames,
                fps=fps,
                with_audio=True,
                synced=True,
                timeline_entries=timeline_entries,
                balloon_text=say_text,
            )
            if cached:
                print(f"Animation {animation.name!r} replayed from cache: immediate")
            if timeline_cached:
                print(f"Animation {animation.name!r} timeline replayed from cache: immediate")
            player.wait_closed()
        finally:
            player.close()


class _FSMRuntimeBridge:
    def __init__(self, catalog_runtime, player):
        self.catalog_runtime = catalog_runtime
        self.player = player

    def play(self, animation_name, frames, fps=10.0, with_audio=False, synced=False, timeline_entries=None, balloon_text=None, loop=True, on_complete=None):
        self.player.play(
            animation_name,
            frames,
            fps=fps,
            with_audio=with_audio,
            synced=synced,
            timeline_entries=timeline_entries,
            balloon_text=balloon_text,
            loop=loop,
            on_complete=on_complete,
        )

    def say(self, text):
        self.player.say(text)

    def stop(self):
        self.player.stop()

    def stop_current_action(self):
        stop_current = getattr(self.player, "stop_current_action", None)
        if callable(stop_current):
            stop_current()
            return
        self.player.stop()

    def move(self, x, y):
        self.player.move(x, y)

    def set_overlay(self, enabled, x=None, y=None):
        self.player.set_overlay(enabled, x=x, y=y)

    def set_balloon_text_progress(self, text: str) -> None:
        self.player.set_balloon_text_progress(text)

    def set_mouth_overlay(self, mouth) -> None:
        self.player.set_mouth_overlay(mouth)

    def clear_speech_visuals(self) -> None:
        self.player.clear_speech_visuals()

    def status(self):
        return self.player.status()

    def available_animation_names(self):
        return self.catalog_runtime.available_animation_names()

    def available_state_names(self):
        return self.catalog_runtime.available_state_names()

    def render_animation(self, animation_name):
        return self.catalog_runtime.render_animation(animation_name)

    def animation_timeline(self, animation_name):
        return self.catalog_runtime.animation_timeline(animation_name)

    def animation_analysis(self, animation_name):
        return self.catalog_runtime.animation_analysis(animation_name)

    def find_animation(self, animation_name):
        return self.catalog_runtime.find_animation(animation_name)

    def list_states(self):
        return self.catalog_runtime.list_states()

class AnimationPlayer(object):
    def __init__(self, runtime):
        self.runtime = runtime
        self._commands = queue.Queue()
        self._thread = None
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._startup_error = None
        self._startup_error_detail = None
        self._status_lock = threading.Lock()
        self._default_sync_debug = False
        self._default_interaction_enabled = True
        self._service_debug = False
        self._default_overlay_enabled = False
        self._default_overlay_x = None
        self._default_overlay_y = None
        self.progressive_balloon_text = None
        self.current_mouth_overlay = None
        self.speech_visuals_active = False
        self._status = {
            "mode": "idle",
            "animation_name": None,
            "fps": 0.0,
            "synced": False,
            "with_audio": False,
            "debug_sync": False,
            "overlay": False,
            "overlay_applied": False,
            "overlay_stable": False,
            "player_created": False,
            "window_created": False,
            "x": None,
            "y": None,
            "window_width": None,
            "window_height": None,
            "frame_index": None,
            "frame_count": 0,
            "balloon_text": None,
            "current_audio_index": None,
            "progressive_balloon_text": None,
            "current_mouth_overlay": None,
            "speech_visuals_active": False,
            "window_resizable": True,
            "overlay_dragging": False,
            "interaction_enabled": self._default_interaction_enabled,
            "last_interaction": None,
        }
        self.fsm = ClippyStateMachine(_FSMRuntimeBridge(self.runtime, self))

    def _start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._closed.clear()
        self._startup_error = None
        self._startup_error_detail = None
        self._thread = threading.Thread(
            target=self._run,
            name="AgentPyAnimationPlayer",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=5)
        if self._startup_error is not None:
            detail = self._startup_error_detail or f"{type(self._startup_error).__name__}: {self._startup_error}"
            print(f"details: {detail}", file=sys.stderr)
            raise RuntimeError(f"Unable to start animation player (details: {detail})") from self._startup_error

    def _run(self):
        try:
            import tkinter as tk
            from tkinter import simpledialog
            from PIL import ImageTk
        except ImportError as exc:
            self._startup_error = exc
            self._startup_error_detail = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            self._closed.set()
            return

        try:
                root = tk.Tk()
                try:
                    root.withdraw()
                except Exception:
                    pass
                normal_bg = root.cget("bg")
                root.configure(bg=normal_bg)
                label = tk.Label(root, borderwidth=0, highlightthickness=0, bg=normal_bg)
                label.pack()
                state = {
                    "frames": [],
                    "timeline_entries": [],
                    "index": 0,
                    "current_frame_index": None,
                        "fps": 10.0,
                        "interval_ms": 100,
                        "playing": False,
                        "with_audio": False,
                        "synced": False,
                        "debug_sync": self._default_sync_debug,
                        "overlay_enabled": self._default_overlay_enabled,
                    "window_x": self._default_overlay_x,
                    "window_y": self._default_overlay_y,
                    "window_width": None,
                    "window_height": None,
                    "content_width": None,
                    "content_height": None,
                    "current_audio": None,
                    "current_audio_index": None,
                    "base_frame": None,
                    "balloon_text": None,
                    "photo": None,
                    "animation_name": None,
                    "loop": True,
                    "on_complete": None,
                    "after_id": None,
                    "generation": 0,
                    "mode": "idle",
                    "window_created": False,
                    "window_resizable": True,
                    "overlay_dragging": False,
                    "interaction_enabled": self._default_interaction_enabled,
                    "last_interaction": None,
                    "overlay_applied": False,
                    "overlay_stable": False,
                    "initial_geometry_applied": False,
                    "last_geometry": None,
                    "drag_start_x": None,
                    "drag_start_y": None,
                    "drag_origin_x": None,
                    "drag_origin_y": None,
                    "mouse_press_x": None,
                    "mouse_press_y": None,
                    "mouse_press_root_x": None,
                    "mouse_press_root_y": None,
                    "mouse_press_time": None,
                    "mouse_press_visible": False,
                    "mouse_gesture_was_drag": False,
                    "suppress_single_click_until": 0.0,
                    "pending_click_after_id": None,
                    "display_frame": None,
                }
                transparent_key = "#010203"

                def publish_status():
                    snapshot = {
                        "mode": state["mode"],
                        "animation_name": state["animation_name"],
                        "fps": state["fps"],
                        "synced": state["synced"],
                        "with_audio": state["with_audio"],
                        "debug_sync": state["debug_sync"],
                        "overlay": state["overlay_enabled"],
                        "overlay_applied": state["overlay_applied"],
                        "overlay_stable": state["overlay_stable"],
                        "player_created": True,
                        "window_created": state["window_created"],
                        "x": state["window_x"],
                        "y": state["window_y"],
                        "window_width": state["window_width"],
                        "window_height": state["window_height"],
                        "frame_index": state["current_frame_index"],
                        "frame_count": len(state["frames"]),
                        "balloon_text": state["balloon_text"],
                        "progressive_balloon_text": self.progressive_balloon_text,
                        "current_mouth_overlay": self.current_mouth_overlay,
                        "speech_visuals_active": self.speech_visuals_active,
                        "current_audio_index": state["current_audio_index"],
                        "window_resizable": state["window_resizable"],
                        "overlay_dragging": state["overlay_dragging"],
                        "interaction_enabled": state["interaction_enabled"],
                        "last_interaction": state["last_interaction"],
                    }
                    with self._status_lock:
                        self._status = snapshot

                def ensure_window_visible():
                    try:
                        if not root.winfo_viewable():
                            root.deiconify()
                    except Exception:
                        try:
                            root.deiconify()
                        except Exception:
                            pass
                    try:
                        root.lift()
                    except Exception:
                        pass

                def apply_window_mode(force=False):
                    desired_overlay = bool(state["overlay_enabled"])
                    if not force and state["overlay_stable"] and state["overlay_applied"] == desired_overlay:
                        return
                    if self._service_debug:
                        print(f"overlay_mode_change source=apply_window_mode mode={'overlay_on' if desired_overlay else 'overlay_off'}")
                    if desired_overlay:
                        try:
                            root.configure(bg=transparent_key)
                            label.configure(bg=transparent_key)
                            root.overrideredirect(True)
                        except Exception:
                            pass
                        try:
                            root.attributes("-topmost", True)
                        except Exception:
                            pass
                        try:
                            root.lift()
                        except Exception:
                            pass
                        try:
                            root.wm_attributes("-transparentcolor", transparent_key)
                        except Exception:
                            pass
                    else:
                        try:
                            root.configure(bg=normal_bg)
                            label.configure(bg=normal_bg)
                            root.overrideredirect(False)
                        except Exception:
                            pass
                        try:
                            root.attributes("-topmost", True)
                        except Exception:
                            pass
                        try:
                            root.lift()
                        except Exception:
                            pass
                        try:
                            root.wm_attributes("-transparentcolor", "")
                        except Exception:
                            pass
                    try:
                        root.resizable(not desired_overlay, not desired_overlay)
                    except Exception:
                        pass
                    state["window_resizable"] = not desired_overlay
                    state["overlay_applied"] = desired_overlay
                    state["overlay_stable"] = True
                    if self._service_debug:
                        print(f"window_topmost mode={'overlay_on' if desired_overlay else 'overlay_off'} value=True")
                    publish_status()

                publish_status()

                def sync_window_geometry(force_size=False):
                    width = state["content_width"]
                    height = state["content_height"]
                    if width is None or height is None:
                        return
                    if not state["overlay_enabled"]:
                        if force_size and not state["initial_geometry_applied"]:
                            try:
                                geometry = f"{int(width)}x{int(height)}"
                                if state["last_geometry"] != geometry:
                                    root.geometry(geometry)
                                    state["last_geometry"] = geometry
                                state["initial_geometry_applied"] = True
                            except Exception:
                                pass
                        publish_status()
                        return
                    x = state["window_x"]
                    y = state["window_y"]
                    if x is None:
                        try:
                            x = max(0, root.winfo_screenwidth() - int(width) - 24)
                        except Exception:
                            x = 0
                    if y is None:
                        try:
                            y = max(0, root.winfo_screenheight() - int(height) - 64)
                        except Exception:
                            y = 0
                    geometry = f"{int(width)}x{int(height)}+{int(x)}+{int(y)}"
                    if state["last_geometry"] == geometry:
                        publish_status()
                        return
                    try:
                        root.geometry(geometry)
                        state["last_geometry"] = geometry
                    except Exception:
                        pass
                    publish_status()

                def on_configure(event):
                    if event.widget is not root:
                        return
                    state["window_width"] = int(event.width)
                    state["window_height"] = int(event.height)
                    try:
                        state["window_x"] = int(event.x)
                        state["window_y"] = int(event.y)
                    except Exception:
                        pass
                    publish_status()

                def _cancel_pending_click():
                    after_id = state["pending_click_after_id"]
                    if after_id is not None:
                        try:
                            root.after_cancel(after_id)
                        except Exception:
                            pass
                        state["pending_click_after_id"] = None

                def _visible_hit(event):
                    frame = state["display_frame"]
                    if frame is None:
                        return True
                    x = int(event.x)
                    y = int(event.y)
                    if x < 0 or y < 0 or x >= frame.width or y >= frame.height:
                        return False
                    try:
                        pixel = frame.getpixel((x, y))
                    except Exception:
                        return True
                    if isinstance(pixel, tuple):
                        return len(pixel) < 4 or int(pixel[3]) > 16
                    return True

                def _set_last_interaction(kind):
                    state["last_interaction"] = kind
                    publish_status()

                def _available_animation_name(preferred_names):
                    available = {name.casefold(): name for name in self.runtime.available_animation_names()}
                    for name in preferred_names:
                        match = available.get(name.casefold())
                        if match is not None:
                            return match
                    names = self.runtime.available_animation_names()
                    return names[0] if names else None

                def _play_named_animation(animation_name, balloon_text=None, fps=10.0, synced=False):
                    if not animation_name:
                        return False
                    if synced:
                        animation, rendered_frames, _ = self.runtime.render_animation(animation_name)
                        _, timeline_entries, _ = self.runtime.animation_timeline(animation_name)
                        set_animation(
                            animation.name,
                            rendered_frames,
                            fps,
                            with_audio=True,
                            synced=True,
                            timeline_entries=timeline_entries,
                            balloon_text=balloon_text,
                        )
                    else:
                        animation, rendered_frames, _ = self.runtime.render_animation(animation_name)
                        set_animation(
                            animation.name,
                            rendered_frames,
                            fps,
                            with_audio=False,
                            synced=False,
                            timeline_entries=None,
                            balloon_text=balloon_text,
                        )
                    return True

                def _single_click_action():
                    _set_last_interaction("click")
                    if self._service_debug:
                        print("click_target source=ui_click")
                    self.fsm.post_event(
                        EventType.CLICK,
                        {
                            "source": "ui_click",
                        },
                    )

                def _double_click_action():
                    _set_last_interaction("double-click")
                    if self._service_debug:
                        print("click_target source=ui_double_click")
                    self.fsm.post_event(
                        EventType.DOUBLE_CLICK,
                        {
                            "source": "ui_double_click",
                        },
                    )

                def _prompt_show_animation():
                    default_name = _available_animation_name(["Wave", "Greeting", "GetAttention"])
                    name = simpledialog.askstring("Show animation", "Animation name:", initialvalue=default_name, parent=root)
                    if not name:
                        return
                    fps_value = simpledialog.askstring("Show animation", "FPS:", initialvalue="10", parent=root)
                    fps = float(fps_value) if fps_value else 10.0
                    animation, rendered_frames, cached = self.runtime.render_animation(name)
                    if cached:
                        print(f"Animation {animation.name!r} replayed from cache: immediate")
                    set_animation(animation.name, rendered_frames, fps, with_audio=False, synced=False)

                def _prompt_sync_animation():
                    default_name = _available_animation_name(["Explain", "GetAttention", "Wave"])
                    name = simpledialog.askstring("Sync animation", "Animation name:", initialvalue=default_name, parent=root)
                    if not name:
                        return
                    fps_value = simpledialog.askstring("Sync animation", "FPS:", initialvalue="10", parent=root)
                    fps = float(fps_value) if fps_value else 10.0
                    animation, rendered_frames, cached = self.runtime.render_animation(name)
                    _, timeline_entries, timeline_cached = self.runtime.animation_timeline(name)
                    if cached:
                        print(f"Animation {animation.name!r} replayed from cache: immediate")
                    if timeline_cached:
                        print(f"Animation {animation.name!r} timeline replayed from cache: immediate")
                    set_animation(
                        animation.name,
                        rendered_frames,
                        fps,
                        with_audio=True,
                        synced=True,
                        timeline_entries=timeline_entries,
                    )

                def _prompt_say():
                    text = simpledialog.askstring("Say", "Dialogue text:", initialvalue="Bonjour !", parent=root)
                    if text:
                        self.fsm.post_event(
                            EventType.SAY_REQUEST,
                            {
                                "text": text,
                                "preferred_animations": ["Speaking", "Explain", "Acknowledge"],
                            },
                        )

                def _show_context_menu(event):
                    if not state["interaction_enabled"] or not _visible_hit(event):
                        return
                    if self._service_debug:
                        print("click_target source=ui_right_click")
                    self.fsm.post_event(EventType.RIGHT_CLICK, {"x": int(event.x_root), "y": int(event.y_root), "source": "ui_right_click"})
                    menu = tk.Menu(root, tearoff=0)
                    menu.add_command(label="Show animation...", command=_prompt_show_animation)
                    menu.add_command(label="Sync animation...", command=_prompt_sync_animation)
                    menu.add_command(label="Say...", command=lambda: (self.fsm.post_event(EventType.MENU_COMMAND, {"command": "say"}), _prompt_say()))
                    menu.add_command(label="Stop", command=lambda: (self.fsm.post_event(EventType.MENU_COMMAND, {"command": "stop"}), stop_animation()))
                    menu.add_command(
                        label=f"Overlay {'off' if state['overlay_enabled'] else 'on'}",
                        command=lambda: self.fsm.post_event(
                            EventType.HIDE_REQUEST if state["overlay_enabled"] else EventType.SHOW_REQUEST,
                            {"x": state["window_x"], "y": state["window_y"], "source": "menu_overlay_toggle"},
                        ),
                    )
                    menu.add_command(label="Status", command=lambda: print(self.status_text()))
                    menu.add_command(label="Quit", command=close_player)
                    try:
                        menu.tk_popup(int(event.x_root), int(event.y_root))
                    finally:
                        try:
                            menu.grab_release()
                        except Exception:
                            pass
                        self.fsm.post_event(EventType.MENU_CLOSE, {})

                def start_pointer(event):
                    if not _visible_hit(event):
                        return
                    _cancel_pending_click()
                    state["mouse_gesture_was_drag"] = False
                    state["mouse_press_x"] = int(event.x)
                    state["mouse_press_y"] = int(event.y)
                    state["mouse_press_root_x"] = int(event.x_root)
                    state["mouse_press_root_y"] = int(event.y_root)
                    state["mouse_press_time"] = time.monotonic()
                    state["mouse_press_visible"] = True

                def drag_motion(event):
                    if not state["overlay_enabled"] or not state["mouse_press_visible"]:
                        return
                    start_x = state["mouse_press_x"] if state["mouse_press_x"] is not None else int(event.x)
                    start_y = state["mouse_press_y"] if state["mouse_press_y"] is not None else int(event.y)
                    start_root_x = state["mouse_press_root_x"] if state["mouse_press_root_x"] is not None else int(event.x_root)
                    start_root_y = state["mouse_press_root_y"] if state["mouse_press_root_y"] is not None else int(event.y_root)
                    moved_x = abs(int(event.x) - start_x)
                    moved_y = abs(int(event.y) - start_y)
                    if not state["overlay_dragging"] and max(moved_x, moved_y) < 5:
                        return
                    if not state["overlay_dragging"]:
                        state["overlay_dragging"] = True
                        state["mouse_gesture_was_drag"] = True
                        self.fsm.post_event(EventType.DRAG_START, {"position": (int(event.x_root), int(event.y_root))})
                        try:
                            state["drag_origin_x"] = int(root.winfo_x())
                            state["drag_origin_y"] = int(root.winfo_y())
                        except Exception:
                            state["drag_origin_x"] = state["window_x"] or 0
                            state["drag_origin_y"] = state["window_y"] or 0
                        state["drag_start_x"] = start_root_x
                        state["drag_start_y"] = start_root_y
                        _cancel_pending_click()
                    origin_x = state["drag_origin_x"] if state["drag_origin_x"] is not None else 0
                    origin_y = state["drag_origin_y"] if state["drag_origin_y"] is not None else 0
                    new_x = int(origin_x + (int(event.x_root) - start_root_x))
                    new_y = int(origin_y + (int(event.y_root) - start_root_y))
                    state["window_x"] = new_x
                    state["window_y"] = new_y
                    self.fsm.post_event(EventType.DRAG_MOVE, {"position": (new_x, new_y)})
                    try:
                        current_width = state["window_width"] or state["content_width"] or root.winfo_width()
                        current_height = state["window_height"] or state["content_height"] or root.winfo_height()
                        geometry = f"{int(current_width)}x{int(current_height)}+{new_x}+{new_y}"
                        if state["last_geometry"] != geometry:
                            root.geometry(geometry)
                            state["last_geometry"] = geometry
                    except Exception:
                        pass
                    publish_status()

                def stop_drag(event):
                    if not state["overlay_enabled"]:
                        return
                    if not state["overlay_dragging"]:
                        return
                    state["overlay_dragging"] = False
                    state["drag_start_x"] = None
                    state["drag_start_y"] = None
                    state["drag_origin_x"] = None
                    state["drag_origin_y"] = None
                    state["mouse_press_visible"] = False
                    self.fsm.post_event(EventType.DRAG_END, {"position": (state["window_x"], state["window_y"])})
                    publish_status()

                def handle_single_click(event):
                    if not state["interaction_enabled"] or state["overlay_dragging"] or not _visible_hit(event):
                        return
                    if state["mouse_gesture_was_drag"]:
                        return
                    if time.monotonic() < float(state["suppress_single_click_until"] or 0.0):
                        return
                    state["mouse_press_visible"] = False
                    _cancel_pending_click()
                    state["pending_click_after_id"] = root.after(250, lambda: (_single_click_action(), _cancel_pending_click()))

                def handle_double_click(event):
                    if not state["interaction_enabled"] or not _visible_hit(event):
                        return
                    state["mouse_press_visible"] = False
                    _cancel_pending_click()
                    state["suppress_single_click_until"] = time.monotonic() + 0.35
                    _double_click_action()

                def handle_right_click(event):
                    if not state["interaction_enabled"]:
                        return
                    if _visible_hit(event):
                        _cancel_pending_click()
                        _set_last_interaction("right-click")
                        _show_context_menu(event)

                root.bind("<Configure>", on_configure)
                label.bind("<ButtonPress-1>", start_pointer)
                label.bind("<B1-Motion>", drag_motion)
                label.bind("<ButtonRelease-1>", stop_drag)
                label.bind("<ButtonRelease-1>", handle_single_click, add="+")
                label.bind("<Double-Button-1>", handle_double_click)
                label.bind("<ButtonPress-3>", handle_right_click)

                state["window_created"] = True
                publish_status()
                apply_window_mode()
                sync_window_geometry(force_size=True)

                def cancel_tick():
                    after_id = state["after_id"]
                    if after_id is not None:
                        try:
                            root.after_cancel(after_id)
                        except Exception:
                            pass
                        state["after_id"] = None

                def stop_current_audio():
                    current_audio = state["current_audio"]
                    if current_audio is not None:
                        try:
                            current_audio.stop()
                        except Exception:
                            pass
                        state["current_audio"] = None
                        state["current_audio_index"] = None

                def stop_animation(repost_stop_request=True):
                    _cancel_pending_click()
                    cancel_tick()
                    if repost_stop_request:
                        self.fsm.post_event(EventType.STOP_REQUEST, {"source": "player_stop_animation"})
                    stop_current_audio()
                    self.clear_speech_visuals()
                    state["playing"] = False
                    state["mode"] = "idle"
                    state["loop"] = True
                    state["on_complete"] = None
                    state["generation"] += 1
                    state["current_frame_index"] = None
                    publish_status()

                def finish_current_action():
                    callback = state.get("on_complete")
                    state["on_complete"] = None
                    stop_current_audio()
                    state["playing"] = False
                    state["mode"] = "idle"
                    state["generation"] += 1
                    state["current_frame_index"] = None
                    publish_status()
                    if callable(callback):
                        try:
                            callback()
                        except Exception as exc:
                            print(f"Completion callback error: {exc}")

                def current_audio_is_playing():
                    current_audio = state["current_audio"]
                    if current_audio is None:
                        return False
                    checker = getattr(current_audio, "is_playing", None)
                    if callable(checker):
                        try:
                            return bool(checker())
                        except Exception:
                            return False
                    return True

                def play_frame_audio(audio_index):
                    if audio_index in (None, 65535):
                        return
                    if state["current_audio"] is not None and state["current_audio_index"] == audio_index and current_audio_is_playing():
                        return
                    if state["current_audio"] is not None:
                        stop_current_audio()
                    try:
                        state["current_audio"] = self.runtime.play_audio_clip(audio_index)
                        state["current_audio_index"] = audio_index
                    except Exception as exc:
                        print(f"Audio playback error: {exc}")

                def redraw_current_frame():
                    base_frame = state["base_frame"]
                    if base_frame is None:
                        return
                    display_frame = base_frame
                    if self.speech_visuals_active and self.current_mouth_overlay is not None:
                        display_frame = self.runtime.compose_dynamic_mouth_frame(
                            display_frame,
                            animation_name=state["animation_name"],
                            frame_index=state["current_frame_index"],
                            mouth=self.current_mouth_overlay,
                        )
                    balloon_text = self.progressive_balloon_text if self.progressive_balloon_text is not None else state["balloon_text"]
                    if balloon_text:
                        display_frame = self.runtime.compose_balloon_frame(display_frame, balloon_text)
                    photo = ImageTk.PhotoImage(display_frame)
                    state["photo"] = photo
                    state["display_frame"] = display_frame
                    state["content_width"], state["content_height"] = display_frame.size
                    label.configure(image=photo, bg=transparent_key if state["overlay_enabled"] else normal_bg)
                    sync_window_geometry(force_size=True)
                    if state["overlay_enabled"] or display_frame.getbbox() is not None:
                        ensure_window_visible()

                def frame_has_visible_pixels(frame):
                    if frame is None:
                        return False
                    try:
                        alpha = frame.getchannel("A")
                    except Exception:
                        try:
                            return frame.getbbox() is not None
                        except Exception:
                            return True
                    try:
                        return alpha.getbbox() is not None
                    except Exception:
                        return True

                def schedule_tick(delay_ms=None, generation=None):
                    if state["playing"] and state["frames"]:
                        token = state["generation"] if generation is None else generation
                        state["after_id"] = root.after(int(delay_ms or state["interval_ms"]), lambda tok=token: tick(tok))

                def tick(generation=None):
                    if generation is not None and generation != state["generation"]:
                        return
                    state["after_id"] = None
                    if not state["playing"] or not state["frames"]:
                        return
                    frame_item = state["frames"][state["index"]]
                    frame_index = state["index"]
                    if len(frame_item) >= 3:
                        frame, _duration_ms, audio_index = frame_item[:3]
                    else:
                        frame, _duration_ms = frame_item[:2]
                        audio_index = 65535
                    entry = None
                    if state["synced"] and state["timeline_entries"]:
                        entry = state["timeline_entries"][state["index"]]
                    frame_is_visible = frame_has_visible_pixels(frame)
                    if frame_is_visible or state["base_frame"] is None:
                        state["base_frame"] = frame
                    redraw_current_frame()
                    if state["with_audio"] and audio_index not in (None, 65535):
                        play_frame_audio(audio_index)
                    if state["synced"] and entry is not None:
                        overlay_text = ", ".join(entry["overlay_types"]) if entry["overlay_types"] else "-"
                        audio_text = "-" if audio_index in (None, 65535) else str(audio_index)
                        root.title(
                            f"{state['animation_name']} | frame {entry['frame_index']} | "
                            f"audio {audio_text} | overlay {overlay_text} | {state['fps']:g} fps"
                        )
                        if state["debug_sync"]:
                            print(f"Frame {entry['frame_index']} | audio={audio_text} | overlay={overlay_text}", file=sys.stderr)
                    state["current_frame_index"] = frame_index
                    state["last_frame_was_visible"] = bool(frame_is_visible)
                    publish_status()
                    if state["overlay_enabled"] or frame_is_visible:
                        ensure_window_visible()
                    next_delay_ms = entry["duration_ms"] if entry is not None else state["interval_ms"]
                    state["index"] += 1
                    if not state["loop"] and state["index"] >= len(state["frames"]):
                        state["after_id"] = root.after(max(1, int(next_delay_ms)), lambda tok=generation: finish_current_action() if tok == state["generation"] else None)
                        return
                    state["index"] %= len(state["frames"])
                    schedule_tick(next_delay_ms, generation)

                def set_animation(animation_name, frames, fps, with_audio=False, synced=False, timeline_entries=None, balloon_text=None, loop=True, on_complete=None):
                    _cancel_pending_click()
                    cancel_tick()
                    stop_current_audio()
                    state["generation"] += 1
                    state["frames"] = list(frames)
                    state["timeline_entries"] = list(timeline_entries or [])
                    state["index"] = 0
                    state["current_frame_index"] = None
                    state["fps"] = fps
                    state["interval_ms"] = max(1, int(round(1000.0 / fps)))
                    state["playing"] = True
                    state["with_audio"] = with_audio
                    state["synced"] = synced
                    state["animation_name"] = animation_name
                    state["loop"] = bool(loop)
                    state["on_complete"] = on_complete
                    state["mode"] = "sync" if synced else "animation"
                    state["initial_geometry_applied"] = False
                    state["balloon_text"] = None if balloon_text is None else str(balloon_text)
                    root.title(f"{animation_name} - {self.runtime.character_path.name} @ {fps:g} fps")
                    publish_status()
                    tick(state["generation"])
                    if state["overlay_enabled"]:
                        ensure_window_visible()

                def say_balloon(text):
                    _cancel_pending_click()
                    state["balloon_text"] = None if text is None else str(text)
                    if text is not None and state["base_frame"] is None and not state["frames"]:
                        try:
                            state["base_frame"] = self.runtime.first_image()
                        except Exception:
                            pass
                    redraw_current_frame()
                    publish_status()

                def set_debug_sync(enabled):
                    state["debug_sync"] = bool(enabled)
                    publish_status()

                def set_overlay_mode(enabled, x=None, y=None):
                    if self._service_debug:
                        print(f"overlay_mode_change source=set_overlay_mode mode={'overlay_on' if enabled else 'overlay_off'}")
                    state["overlay_enabled"] = bool(enabled)
                    if x is not None:
                        state["window_x"] = int(x)
                    if y is not None:
                        state["window_y"] = int(y)
                    if state["overlay_enabled"] and state["base_frame"] is None and not state["frames"]:
                        try:
                            state["base_frame"] = self.runtime.first_image()
                        except Exception:
                            pass
                    state["initial_geometry_applied"] = False
                    state["overlay_stable"] = False
                    apply_window_mode(force=True)
                    if state["base_frame"] is not None:
                        redraw_current_frame()
                    else:
                        sync_window_geometry()
                    if not state["overlay_enabled"]:
                        if state["base_frame"] is not None or state["frames"]:
                            ensure_window_visible()
                    publish_status()

                def move_overlay(x, y):
                    state["window_x"] = int(x)
                    state["window_y"] = int(y)
                    sync_window_geometry()

                def close_player():
                    _cancel_pending_click()
                    cancel_tick()
                    stop_current_audio()
                    self.clear_speech_visuals()
                    state["playing"] = False
                    state["mode"] = "idle"
                    state["generation"] += 1
                    state["window_created"] = False
                    if root.winfo_exists():
                        root.destroy()

                def process_commands():
                    while True:
                        try:
                            command = self._commands.get_nowait()
                        except queue.Empty:
                            break
                        kind = command[0]
                        if kind == "set":
                            _, animation_name, frames, fps, with_audio, synced, timeline_entries, balloon_text, loop, on_complete = command
                            set_animation(
                                animation_name,
                                frames,
                                fps,
                                with_audio=with_audio,
                                synced=synced,
                                timeline_entries=timeline_entries,
                                balloon_text=balloon_text,
                                loop=loop,
                                on_complete=on_complete,
                            )
                        elif kind == "say":
                            _, text = command
                            say_balloon(text)
                        elif kind == "stop":
                            stop_animation()
                        elif kind == "stop-runtime":
                            stop_animation(repost_stop_request=False)
                        elif kind == "debug-sync":
                            _, enabled = command
                            set_debug_sync(enabled)
                        elif kind == "interaction":
                            _, enabled = command
                            state["interaction_enabled"] = bool(enabled)
                            if not state["interaction_enabled"]:
                                _cancel_pending_click()
                            publish_status()
                        elif kind == "overlay":
                            _, enabled, x, y = command
                            set_overlay_mode(enabled, x=x, y=y)
                        elif kind == "move":
                            _, x, y = command
                            move_overlay(x, y)
                        elif kind == "quit":
                            close_player()
                            self._closed.set()
                            return
                    if root.winfo_exists():
                        frame_before = state.get("current_frame_index")
                        active_visuals = bool(
                            state.get("playing")
                            or self.speech_visuals_active
                            or state.get("current_audio") is not None
                        )
                        service_delay_ms = 30 if active_visuals else 1000
                        try:
                            self.fsm.tick()
                        except Exception:
                            pass
                        if self._service_debug:
                            service_state = (
                                bool(state["playing"]),
                                bool(self.speech_visuals_active),
                                bool(state.get("current_audio") is not None),
                                state.get("mode"),
                                frame_before,
                                state.get("animation_name"),
                            )
                            last_service_state = state.get("last_service_state")
                            if service_state != last_service_state:
                                queue_before = None
                                if getattr(self.fsm, "_event_queue", None) is not None:
                                    try:
                                        queue_before = len(self.fsm._event_queue)
                                    except Exception:
                                        queue_before = "unavailable"
                                print(
                                    f"[service] state={service_state} queue={queue_before} "
                                    f"delay_ms={service_delay_ms}"
                                )
                                state["last_service_state"] = service_state
                                state["last_service_heartbeat"] = time.monotonic()
                            now = time.monotonic()
                            last_heartbeat = float(state.get("last_service_heartbeat") or 0.0)
                            if now - last_heartbeat >= 5.0:
                                queue_after = None
                                if getattr(self.fsm, "_event_queue", None) is not None:
                                    try:
                                        queue_after = len(self.fsm._event_queue)
                                    except Exception:
                                        queue_after = "unavailable"
                                print(
                                    f"[service] heartbeat state={state.get('last_service_state')} "
                                    f"queue={queue_after} delay_ms={service_delay_ms}"
                                )
                                state["last_service_heartbeat"] = now
                        root.after(service_delay_ms, process_commands)

                root.protocol("WM_DELETE_WINDOW", close_player)
                self._ready.set()
                root.after(0, process_commands)
                root.mainloop()
        except Exception as exc:
            self._startup_error = exc
            self._startup_error_detail = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            self._closed.set()

    def play(self, animation_name, frames, fps=10.0, with_audio=False, synced=False, timeline_entries=None, balloon_text=None, loop=True, on_complete=None):
        self._start()
        self._commands.put(("set", animation_name, list(frames), float(fps), bool(with_audio), bool(synced), list(timeline_entries or []), balloon_text, bool(loop), on_complete))

    def set_animation(self, animation_name, frames, fps=10.0, with_audio=False, synced=False, timeline_entries=None, balloon_text=None, loop=True, on_complete=None):
        self.play(animation_name, frames, fps=fps, with_audio=with_audio, synced=synced, timeline_entries=timeline_entries, balloon_text=balloon_text, loop=loop, on_complete=on_complete)

    def say(self, text):
        self._start()
        self._commands.put(("say", text))

    def clear_balloon(self):
        if self._thread is None or not self._thread.is_alive():
            return
        self.say(None)

    def set_balloon_text_progress(self, text: str) -> None:
        self._start()
        self.progressive_balloon_text = None if text is None else str(text)
        self.speech_visuals_active = bool(self.progressive_balloon_text is not None or self.current_mouth_overlay is not None)
        with self._status_lock:
            self._status["progressive_balloon_text"] = self.progressive_balloon_text
            self._status["speech_visuals_active"] = self.speech_visuals_active

    def set_mouth_overlay(self, mouth) -> None:
        self._start()
        if mouth is None or mouth == "":
            self.current_mouth_overlay = None
        elif isinstance(mouth, str):
            self.current_mouth_overlay = mouth
        else:
            self.current_mouth_overlay = getattr(mouth, "mouth", getattr(mouth, "name", str(mouth)))
        self.speech_visuals_active = bool(self.progressive_balloon_text is not None or self.current_mouth_overlay is not None)
        with self._status_lock:
            self._status["current_mouth_overlay"] = self.current_mouth_overlay
            self._status["speech_visuals_active"] = self.speech_visuals_active

    def clear_speech_visuals(self):
        self._start()
        self.progressive_balloon_text = None
        self.current_mouth_overlay = None
        self.speech_visuals_active = False
        with self._status_lock:
            self._status["progressive_balloon_text"] = None
            self._status["current_mouth_overlay"] = None
            self._status["speech_visuals_active"] = False

    def stop(self):
        if self._thread is not None and self._thread.is_alive():
            self._commands.put(("stop",))

    def stop_current_action(self):
        if self._thread is not None and self._thread.is_alive():
            self._commands.put(("stop-runtime",))

    def set_sync_debug(self, enabled):
        self._default_sync_debug = bool(enabled)
        with self._status_lock:
            self._status["debug_sync"] = self._default_sync_debug
            self._status["player_created"] = self._thread is not None and self._thread.is_alive()
        if self._thread is not None and self._thread.is_alive():
            self._commands.put(("debug-sync", bool(enabled)))

    def set_interaction(self, enabled):
        self._default_interaction_enabled = bool(enabled)
        with self._status_lock:
            self._status["interaction_enabled"] = self._default_interaction_enabled
            self._status["player_created"] = self._thread is not None and self._thread.is_alive()
        if self._thread is not None and self._thread.is_alive():
            self._commands.put(("interaction", bool(enabled)))

    def set_service_debug(self, enabled):
        self._service_debug = bool(enabled)

    def set_overlay(self, enabled, x=None, y=None):
        self._default_overlay_enabled = bool(enabled)
        if x is not None:
            self._default_overlay_x = int(x)
        if y is not None:
            self._default_overlay_y = int(y)
        with self._status_lock:
            self._status["overlay"] = self._default_overlay_enabled
            self._status["x"] = self._default_overlay_x
            self._status["y"] = self._default_overlay_y
            self._status["player_created"] = self._thread is not None and self._thread.is_alive()
        if self._thread is not None and self._thread.is_alive():
            self._commands.put(("overlay", bool(enabled), self._default_overlay_x, self._default_overlay_y))

    def move(self, x, y):
        self._default_overlay_x = int(x)
        self._default_overlay_y = int(y)
        with self._status_lock:
            self._status["x"] = self._default_overlay_x
            self._status["y"] = self._default_overlay_y
            self._status["player_created"] = self._thread is not None and self._thread.is_alive()
        if self._thread is not None and self._thread.is_alive():
            self._commands.put(("move", self._default_overlay_x, self._default_overlay_y))

    def status(self):
        with self._status_lock:
            return dict(self._status)

    def status_text(self):
        status = self.status()
        lines = [
            "Player status",
            "-------------",
            f"Player: {'created' if status['player_created'] else 'not created'}",
            f"Window: {'created' if status['window_created'] else 'not created'}",
            f"Mode: {status['mode']}",
            f"Animation: {status['animation_name'] or '-'}",
            f"FPS: {status['fps']:g}" if status["fps"] else "FPS: -",
            f"Synced: {'yes' if status['synced'] else 'no'}",
            f"Audio: {'yes' if status['with_audio'] else 'no'}",
            f"Debug sync: {'on' if status['debug_sync'] else 'off'}",
            f"Interaction: {'on' if status.get('interaction_enabled') else 'off'}",
            f"Interaction profile: {getattr(getattr(self.fsm, 'context', None), 'interaction_profile', '-')}",
            f"Last interaction: {status.get('last_interaction') or '-'}",
            f"Overlay requested: {'on' if status['overlay'] else 'off'}",
            f"Overlay applied: {'on' if status.get('overlay_applied') else 'off'}",
            f"Overlay stable: {'yes' if status.get('overlay_stable') else 'no'}",
            f"Resizable: {'yes' if status['window_resizable'] else 'no'}",
            f"Overlay drag: {'yes' if status['overlay_dragging'] else 'no'}",
            f"Position: {status['x'] if status['x'] is not None else '-'}, {status['y'] if status['y'] is not None else '-'}",
            f"Window size: {status['window_width'] if status['window_width'] is not None else '-'} x {status['window_height'] if status['window_height'] is not None else '-'}",
            f"Current frame: {status['frame_index'] if status['frame_index'] is not None else '-'}",
            f"Frames: {status['frame_count']}",
            f"Balloon: {status['balloon_text'] if status['balloon_text'] is not None else '-'}",
            f"Progressive balloon: {status.get('progressive_balloon_text') if status.get('progressive_balloon_text') is not None else '-'}",
            f"Mouth overlay: {status.get('current_mouth_overlay') if status.get('current_mouth_overlay') is not None else '-'}",
            f"Speech visuals: {'on' if status.get('speech_visuals_active') else 'off'}",
            f"Active audio: {status['current_audio_index'] if status['current_audio_index'] is not None else '-'}",
        ]
        return "\n".join(lines)

    def close(self):
        if self._thread is None:
            return
        if self._thread.is_alive():
            self._commands.put(("quit",))
            self._closed.wait(timeout=5)
            self._thread.join(timeout=5)
        with self._status_lock:
            self._status["player_created"] = False
            self._status["window_created"] = False

    def wait_closed(self):
        self._closed.wait()

def _build_first_image(character_path):
    runtime = get_runtime(character_path)
    return runtime.agent, runtime.agent.data.acsimageinfo[0].image_data, runtime.first_image()

def _safe_filename(name, extension):
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    if not base:
        base = "animation"
    return f"{base}{extension}"

def _load_animation(character_path, animation_name):
    runtime = get_runtime(character_path)
    animation = runtime.find_animation(animation_name)
    return runtime.agent, animation

def _sprite_to_rgba(character, image_index, sprite_cache):
    runtime = character if isinstance(character, AgentRuntime) else get_runtime(character)
    return runtime._sprite_rgba(image_index)

def _render_animation_frame(character, frame, sprite_cache):
    runtime = character if isinstance(character, AgentRuntime) else get_runtime(character)
    return runtime._compose_frame(frame)

def _build_animation_frames(character, animation):
    runtime = character if isinstance(character, AgentRuntime) else get_runtime(character)
    cache_key = animation.name.casefold() if hasattr(animation, "name") else str(animation).casefold()
    cached = runtime.animation_cache.get(cache_key)
    if cached is not None:
        return cached
    if hasattr(animation, "animation_data"):
        rendered_frames = []
        for frame in animation.animation_data.frames:
            rendered_frames.append((runtime._compose_frame(frame), max(1, int(round((frame.frame_duration_csecs or 1) * 10)))))
        runtime.animation_cache[cache_key] = rendered_frames
        return rendered_frames
    raise TypeError("animation must be an animation object")

def list_animations(character_path: str) -> None:
    runtime = get_runtime(character_path)
    print("Available animations:")
    for animation in runtime.list_animations():
        frame_count = len(animation.animation_data.frames)
        print(f"- {animation.name} ({frame_count} frames)")

def list_states(character_path: str) -> None:
    runtime = get_runtime(character_path)
    print("Available states:")
    for state in runtime.list_states():
        frame_count = len(state.animations)
        print(f"- {state.name} ({frame_count} animations)")

def list_audio(character_path: str) -> None:
    runtime = get_runtime(character_path)
    runtime.print_audio_clips()

def audio_usage(character_path: str) -> None:
    runtime = get_runtime(character_path)
    runtime.print_audio_usage()

def audio_info(character_path: str) -> None:
    runtime = get_runtime(character_path)
    runtime.print_audio_info()

def balloon_info(character_path: str) -> None:
    runtime = get_runtime(character_path)
    runtime.print_balloon_info()

def timeline(character_path: str, animation_name: str) -> None:
    runtime = get_runtime(character_path)
    runtime.print_timeline(animation_name)

def analyze_animation(character_path: str, animation_name: str) -> None:
    runtime = get_runtime(character_path)
    runtime.print_animation_analysis(animation_name)

def extract_audio(character_path: str, audio_index: int, output_path: str | None = None) -> Path:
    runtime = get_runtime(character_path)
    output = runtime.extract_audio_clip(audio_index, output_path=output_path)
    print(f"Saved {output}")
    return output

def play_audio(character_path: str, audio_index: int):
    runtime = get_runtime(character_path)
    try:
        play_object = runtime.play_audio_clip(audio_index)
    except (RuntimeError, ValueError) as exc:
        print(str(exc))
        return None
    print(f"Playing audio {audio_index}")
    return play_object

def show_balloon(character_path: str, text: str, overlay: bool = False, x: int | None = None, y: int | None = None) -> None:
    runtime = get_runtime(character_path)
    runtime.show_balloon(text, overlay=overlay, x=x, y=y)

def show_animation(character_path: str, animation_name: str, fps: float = 10.0, with_audio: bool = False, say_text: str | None = None, overlay: bool = False, x: int | None = None, y: int | None = None) -> None:
    runtime = get_runtime(character_path)
    runtime.show_animation(animation_name, fps=fps, with_audio=with_audio, say_text=say_text, overlay=overlay, x=x, y=y)

def show_state(character_path: str, state_name: str, fps: float = 10.0, with_audio: bool = False, say_text: str | None = None, overlay: bool = False, x: int | None = None, y: int | None = None) -> None:
    runtime = get_runtime(character_path)
    runtime.show_state(state_name, fps=fps, with_audio=with_audio, say_text=say_text, overlay=overlay, x=x, y=y)

def play_synced(character_path: str, animation_name: str, fps: float = 10.0, say_text: str | None = None, overlay: bool = False, x: int | None = None, y: int | None = None) -> None:
    runtime = get_runtime(character_path)
    runtime.show_synced_animation(animation_name, fps=fps, say_text=say_text, overlay=overlay, x=x, y=y)

def save_animation_gif(character_path: str, animation_name: str, fps: float = 10.0, output_path: str | None = None) -> Path:
    runtime = get_runtime(character_path)
    return runtime.save_animation_gif(animation_name, fps=fps, output_path=output_path)

def show_first_image(character_path: str) -> None:
    runtime = get_runtime(character_path)
    runtime.show_first_image()

def info(character_path: str) -> None:
    runtime = get_runtime(character_path)
    runtime.print_info()

def interactive_shell(
    character_path: str | None = None,
    overlay: bool = False,
    x: int | None = None,
    y: int | None = None,
    debug_service: bool = False,
) -> None:
    runtime = get_runtime(character_path)
    player = AnimationPlayer(runtime)
    try:
        player.set_service_debug(bool(debug_service))
        player._start()
    except Exception:
        pass
    if overlay or x is not None or y is not None:
        player.set_overlay(True if overlay or x is not None or y is not None else False, x=x, y=y)
    print("Interactive mode. Type 'help' to see available commands.")
    try:
        while True:
            try:
                raw_line = input("agentpy> ")
            except EOFError:
                print()
                return
            except KeyboardInterrupt:
                print("^C")
                player.stop()
                continue
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split()
            command = parts[0].casefold()
            args = parts[1:]
            try:
                if command == "help":
                    _print_interactive_help(args[0] if args else None)
                    continue
                if command in {"quit", "exit"}:
                    return
                if command == "list":
                    print("Available animations:")
                    for animation in runtime.list_animations():
                        print(f"- {animation.name} ({len(animation.animation_data.frames)} frames)")
                    continue
                if command == "states":
                    print("Available states:")
                    for state in runtime.list_states():
                        print(f"- {state.name} ({len(state.animations)} animations)")
                    continue
                if command == "first":
                    current = player.status()
                    if current["overlay"] or current["x"] is not None or current["y"] is not None:
                        first_frame = runtime.first_image()
                        player.play("First", [(first_frame, 100)], fps=1.0, with_audio=False)
                    else:
                        runtime.show_first_image()
                    continue
                if command == "info":
                    runtime.print_info()
                    continue
                if command == "status":
                    print(player.status_text())
                    continue
                if command == "position":
                    current = player.status()
                    print(f"Position: {current['x'] if current['x'] is not None else '-'}, {current['y'] if current['y'] is not None else '-'}")
                    continue
                if command == "audio":
                    list_audio(str(runtime.character_path))
                    continue
                if command == "audio-usage":
                    audio_usage(str(runtime.character_path))
                    continue
                if command == "audio-info":
                    audio_info(str(runtime.character_path))
                    continue
                if command == "balloon-info":
                    balloon_info(str(runtime.character_path))
                    continue
                if command == "timeline":
                    if not args:
                        print("error: timeline requires an animation name")
                        continue
                    timeline(str(runtime.character_path), args[0])
                    continue
                if command == "analyze":
                    if not args:
                        print("error: analyze requires an animation name")
                        continue
                    analyze_animation(str(runtime.character_path), args[0])
                    continue
                if command == "mouth-dump":
                    if not args:
                        print("error: mouth-dump requires an animation name")
                        continue
                    animation_name = args[0]
                    frame_index = int(args[1]) if len(args) > 1 else 12
                    output_dir = args[2] if len(args) > 2 else None
                    print(runtime.dump_mouth_overlay_geometry(animation_name, frame_index=frame_index))
                    source_index, out_dir, variants = runtime.export_mouth_overlay_variants(
                        animation_name,
                        frame_index=frame_index,
                        output_dir=output_dir,
                    )
                    print(f"Rendered mouth variants from frame {source_index} into {out_dir}:")
                    for mouth_index, mouth_name, path in variants:
                        print(f"- {mouth_index}: {mouth_name} -> {path}")
                    continue
                if command == "render-dump":
                    if not args:
                        print("error: render-dump requires an animation name")
                        continue
                    animation_name = args[0]
                    frame_index = int(args[1]) if len(args) > 1 else 0
                    compare_path = args[2] if len(args) > 2 else None
                    print(runtime.dump_render_profile(animation_name, frame_index=frame_index))
                    if compare_path:
                        compare_runtime = AgentRuntime(_resolve_acs_path(compare_path))
                        try:
                            print("")
                            print("Compare target")
                            print("--------------")
                            print(compare_runtime.dump_render_profile(animation_name, frame_index=frame_index))
                        finally:
                            pass
                    continue
                if command == "say":
                    text = raw_line.strip()[3:].strip()
                    if not text:
                        print("error: say requires text")
                        continue
                    player.say(text)
                    continue
                if command == "speak":
                    text = raw_line.strip()[5:].strip()
                    if not text:
                        print("error: speak requires text")
                        continue
                    fsm = getattr(player, "fsm", None)
                    print(f"[speak] player exists: {player is not None}")
                    print(f"[speak] player type: {type(player).__name__} id={id(player)}")
                    print(f"[speak] player.fsm exists: {fsm is not None}")
                    if fsm is not None:
                        print(f"[speak] player.fsm type: {type(fsm).__name__} id={id(fsm)}")
                    if fsm is None or not hasattr(fsm, "post_event"):
                        print("error: speech FSM is not available")
                        continue
                    print("[speak] posting SAY_REQUEST")
                    fsm.post_event(EventType.SAY_REQUEST, {"text": text})
                    queue_length = getattr(fsm, "_event_queue", None)
                    if queue_length is not None:
                        try:
                            print(f"[speak] event queue length after post: {len(queue_length)}")
                        except Exception:
                            print("[speak] event queue length after post: unavailable")
                    print(f"[speak] FSM enabled/initialized: {hasattr(player, 'fsm') and player.fsm is not None}")
                    continue
                if command == "speak-clear":
                    fsm = getattr(player, "fsm", None)
                    print(f"[speak-clear] player exists: {player is not None}")
                    print(f"[speak-clear] player type: {type(player).__name__} id={id(player)}")
                    print(f"[speak-clear] player.fsm exists: {fsm is not None}")
                    if fsm is not None:
                        print(f"[speak-clear] player.fsm type: {type(fsm).__name__} id={id(fsm)}")
                    if fsm is None or not hasattr(fsm, "post_event"):
                        print("error: speech FSM is not available")
                        continue
                    print("[speak-clear] posting STOP_REQUEST")
                    fsm.post_event(EventType.STOP_REQUEST, {"source": "interactive_speak_clear"})
                    if hasattr(player, "clear_speech_visuals"):
                        player.clear_speech_visuals()
                    print("Speech cleared.")
                    continue
                if command in {"listen", "unlisten"}:
                    listen_mode = command == "listen"
                    if args and args[0].casefold() == "status":
                        current = player.status()
                        listening_active = getattr(getattr(player, "fsm", None), "context", None)
                        behavior_name = getattr(listening_active, "behavior", None)
                        behavior_text = behavior_name.name if hasattr(behavior_name, "name") else str(behavior_name or "-")
                        print(f"Listening behavior: {behavior_text}")
                        continue
                    if args and args[0].casefold() not in {"on", "off"}:
                        print("error: listen expects 'on', 'off', or 'status'")
                        continue
                    if args:
                        listen_mode = args[0].casefold() == "on"
                    fsm = getattr(player, "fsm", None)
                    if fsm is None or not hasattr(fsm, "post_event"):
                        print("error: speech FSM is not available")
                        continue
                    source = "interactive_listen_on" if listen_mode else "interactive_listen_off"
                    event_type = EventType.LISTEN_START if listen_mode else EventType.LISTEN_CANCEL
                    if getattr(fsm, "_debug_enabled", lambda: False)():
                        print(f"[listen] posting {event_type.name} source={source}")
                    fsm.post_event(event_type, {"source": source})
                    print("Listening enabled." if listen_mode else "Listening disabled.")
                    continue
                if command == "say-clear":
                    player.clear_balloon()
                    print("Balloon cleared.")
                    continue
                if command == "sync":
                    if not args:
                        print("error: sync requires an animation name")
                        continue
                    animation_name = args[0]
                    fps = float(args[1]) if len(args) > 1 else 10.0
                    animation, rendered_frames, cached = runtime.render_animation(animation_name)
                    _, timeline_entries, timeline_cached = runtime.animation_timeline(animation_name)
                    if cached:
                        print(f"Animation {animation.name!r} replayed from cache: immediate")
                    if timeline_cached:
                        print(f"Animation {animation.name!r} timeline replayed from cache: immediate")
                    player.play(
                        animation.name,
                        rendered_frames,
                        fps=fps,
                        with_audio=True,
                        synced=True,
                        timeline_entries=timeline_entries,
                    )
                    print(f"Playing synced animation {animation.name!r} at {fps:g} fps.")
                    continue
                if command == "debug-sync":
                    if not args:
                        current = player.status()
                        print(f"debug-sync is {'on' if current['debug_sync'] else 'off'}")
                        continue
                    setting = args[0].casefold()
                    if setting not in {"on", "off"}:
                        print("error: debug-sync expects 'on' or 'off'")
                        continue
                    player.set_sync_debug(setting == "on")
                    print(f"debug-sync {'enabled' if setting == 'on' else 'disabled'}.")
                    continue
                if command == "overlay":
                    if not args:
                        current = player.status()
                        print(f"overlay is {'on' if current['overlay'] else 'off'}")
                        continue
                    setting = args[0].casefold()
                    if setting not in {"on", "off"}:
                        print("error: overlay expects 'on' or 'off'")
                        continue
                    player.set_overlay(setting == "on")
                    print(f"overlay {'enabled' if setting == 'on' else 'disabled'}.")
                    continue
                if command == "interact":
                    if not args:
                        current = player.status()
                        print(f"interaction is {'on' if current.get('interaction_enabled') else 'off'}")
                        continue
                    setting = args[0].casefold()
                    if setting not in {"on", "off"}:
                        print("error: interact expects 'on' or 'off'")
                        continue
                    player.set_interaction(setting == "on")
                    print(f"interaction {'enabled' if setting == 'on' else 'disabled'}.")
                    continue
                if command == "move":
                    if len(args) < 2:
                        print("error: move requires x and y")
                        continue
                    player.move(int(args[0]), int(args[1]))
                    print(f"Moved overlay to {args[0]}, {args[1]}")
                    continue
                if command == "extract-audio":
                    if not args:
                        print("error: extract-audio requires an audio index")
                        continue
                    extract_audio(str(runtime.character_path), int(args[0]))
                    continue
                if command == "play-audio":
                    if not args:
                        print("error: play-audio requires an audio index")
                        continue
                    play_audio(str(runtime.character_path), int(args[0]))
                    continue
                if command == "stop":
                    player.stop()
                    print("Animation stopped.")
                    continue
                if command == "show":
                    if not args:
                        print("error: show requires an animation name")
                        continue
                    animation_name = args[0]
                    with_audio = False
                    fps = 10.0
                    remaining = args[1:]
                    if remaining and remaining[-1].casefold() in {"audio", "--with-audio"}:
                        with_audio = True
                        remaining = remaining[:-1]
                    if remaining:
                        fps = float(remaining[0])
                    animation, rendered_frames, cached = runtime.render_animation(animation_name)
                    if cached:
                        print(f"Animation {animation.name!r} replayed from cache: immediate")
                    player.play(animation.name, rendered_frames, fps=fps, with_audio=with_audio)
                    print(f"Playing {animation.name!r} at {fps:g} fps.")
                    continue
                if command == "state":
                    if not args:
                        print("error: state requires a state name")
                        continue
                    state_name = args[0]
                    with_audio = False
                    fps = 10.0
                    remaining = args[1:]
                    if remaining and remaining[-1].casefold() in {"audio", "--with-audio"}:
                        with_audio = True
                        remaining = remaining[:-1]
                    if remaining:
                        fps = float(remaining[0])
                    state, rendered_frames, cached = runtime.render_state(state_name)
                    if cached:
                        print(f"State {state.name!r} replayed from cache: immediate")
                    player.play(state.name, rendered_frames, fps=fps, with_audio=with_audio)
                    print(f"Playing state {state.name!r} at {fps:g} fps.")
                    continue
                if command == "gif":
                    if not args:
                        print("error: gif requires an animation name")
                        continue
                    animation_name = args[0]
                    fps = float(args[1]) if len(args) > 1 else 10.0
                    output = runtime.save_animation_gif(animation_name, fps=fps)
                    print(f"Saved animation GIF to {output}")
                    continue
                print(f"Unknown command: {command}")
                print("Type 'help' to see available commands.")
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                print(f"error: {exc}")
            except KeyboardInterrupt:
                print("^C")
                player.stop()
    finally:
        player.close()

_RUNTIME_CACHE = {}

def get_runtime(character_path=None):
    path = _resolve_acs_path(character_path)
    cache_key = str(path.resolve())
    runtime = _RUNTIME_CACHE.get(cache_key)
    if runtime is None:
        runtime = AgentRuntime(path)
        _RUNTIME_CACHE[cache_key] = runtime
    return runtime

INTERACTIVE_HELP_TOPICS = {
    None: [
        "Available commands:",
        "",
    "Animations:",
        "  list                     -> list all animations",
        "  show <name> [fps] [audio]-> play an animation",
        "",
    "States:",
        "  states                   -> list all states",
        "  state <name> [fps] [audio]-> play a state (sequence of animations)",
        "",
        "Images:",
        "  first                    -> show first image",
        "",
        "Character:",
        "  info                     -> show detailed ACS character information",
        "",
        "Audio:",
        "  audio                    -> list audio clips",
        "  audio-usage              -> show audio usage in animations",
        "  audio-info               -> show detailed audio clip information",
        "  balloon-info             -> show balloon style parameters",
        "  timeline <name>          -> inspect an animation frame by frame",
        "  analyze <name>           -> summarize animation structure and complexity",
        "  sync <name> [fps]        -> play or replace synchronized animation",
        "  debug-sync on|off        -> enable or disable sync frame logging",
        "  overlay on|off           -> enable or disable desktop overlay mode",
        "  move <x> <y>             -> move the overlay window",
        "  position                 -> show the current overlay position",
        "                            -> normal mode windows can be resized",
        "                            -> overlay mode can be dragged with the mouse",
        "  interact on|off          -> enable or disable mouse reactions and menu",
        "                            -> click triggers a reaction",
        "                            -> right click opens the context menu",
        "  say <text>               -> show or replace dialogue text",
        "  speak <text>             -> trigger FSM speech with progressive text and mouth motion",
        "  speak-clear              -> stop FSM speech and clear speech visuals",
        "  listen [on|off|status]   -> toggle or inspect listening state",
        "  unlisten                 -> cancel listening state",
        "  say-clear                -> hide the dialogue bubble",
        "  render-dump <anim> [frame] [compare_path]",
        "                           -> dump render profile, palette, stride, and mouth-bank data",
        "  extract-audio <id>       -> export audio clip",
        "  play-audio <id>          -> play audio clip",
        "",
        "Export:",
        "  gif <name> [fps]         -> export animation to GIF",
        "",
        "Control:",
        "  status                   -> show current playback status",
        "  stop                     -> stop current animation",
        "  quit                     -> exit program",
        "",
        "Help:",
        "  help                     -> show this help message",
        "  help <command>           -> show help for one command",
    ],
    "show": [
        "show <animation_name> [fps] [audio]",
        "Play a specific animation.",
        "Example: show Blink 12 audio",
    ],
    "interact": [
        "interact on|off",
        "Enable or disable mouse reactions and the context menu.",
        "Example: interact off",
    ],
    "state": [
        "state <state_name> [fps] [audio]",
        "Play a state as a sequence of animations.",
        "Example: state Greeting 12 audio",
    ],
    "list": [
        "list",
        "List all available animations.",
    ],
    "states": [
        "states",
        "List all available states.",
    ],
    "first": [
        "first",
        "Show the first image.",
    ],
    "info": [
        "info",
        "Show detailed ACS character information.",
    ],
    "audio": [
        "audio",
        "List audio clips.",
    ],
    "audio-usage": [
        "audio-usage",
        "Show audio usage in animations.",
    ],
    "audio-info": [
        "audio-info",
        "Show detailed technical information for each audio clip.",
    ],
    "balloon-info": [
        "balloon-info",
        "Show the ACS balloon style parameters.",
    ],
    "say": [
        "say <text>",
        "Show or replace dialogue text in the current bubble.",
        "Example: say Bonjour, je suis Clippy",
    ],
    "speak": [
        "speak <text>",
        "Trigger FSM speech with progressive text and mouth motion.",
        "Example: speak Bonjour, je suis Clippy",
    ],
    "speak-clear": [
        "speak-clear",
        "Stop FSM speech and clear speech visuals.",
    ],
    "listen": [
        "listen [on|off|status]",
        "Enter or exit listening mode without auto-injecting speech text.",
        "Example: listen on",
        "Example: listen status",
    ],
    "unlisten": [
        "unlisten",
        "Cancel listening mode.",
        "Example: unlisten",
    ],
    "say-clear": [
        "say-clear",
        "Hide the current dialogue bubble without closing the window.",
    ],
    "render-dump": [
        "render-dump <animation_name> [frame_index] [compare_character_path]",
        "Dump render profile, palette, stride, and mouth-bank data.",
        "Example: render-dump Read 12",
        "Example: render-dump Read 12 ..\\Merlin.acs",
    ],
        "timeline": [
        "timeline <animation_name>",
        "Show a frame-by-frame timeline for one animation.",
        "Example: timeline Blink",
    ],
    "analyze": [
        "analyze <animation_name>",
        "Show a structured summary of one animation.",
        "Example: analyze Explain",
    ],
    "mouth-dump": [
        "mouth-dump <animation_name> [frame_index] [output_dir]",
        "Dump and render the 7 mouth overlays for a frame source.",
        "Example: mouth-dump Read 12",
    ],
    "sync": [
        "sync <animation_name> [fps]",
        "Play or replace a synchronized animation.",
        "Example: sync Blink 12",
    ],
    "overlay": [
        "overlay on|off",
        "Enable or disable desktop overlay mode.",
        "Example: overlay on",
    ],
    "move": [
        "move <x> <y>",
        "Move the overlay window to screen coordinates.",
        "Example: move 1200 700",
    ],
    "position": [
        "position",
        "Show the current overlay position.",
    ],
    "debug-sync": [
        "debug-sync on|off",
        "Enable or disable per-frame sync logging.",
        "Example: debug-sync off",
    ],
    "status": [
        "status",
        "Show the current playback status.",
    ],
    "extract-audio": [
        "extract-audio <id>",
        "Export an audio clip.",
        "Example: extract-audio 2",
    ],
    "play-audio": [
        "play-audio <id>",
        "Play an audio clip if simpleaudio is installed.",
        "Example: play-audio 2",
    ],
    "gif": [
        "gif <animation_name> [fps]",
        "Export an animation to a GIF file.",
        "Example: gif Blink 12",
    ],
    "stop": [
        "stop",
        "Stop the current animation without closing the window.",
    ],
    "quit": [
        "quit",
        "Exit the interactive shell and close the window.",
    ],
}

CLI_OPTION_LINES = [
    "Options:",
    "  --info",
    "  --list-audio",
    "  --audio-usage",
    "  --audio-info",
    "  --balloon-info",
    "  --timeline <name>",
    "  --analyze-animation <name>",
    "  --play-synced <name>",
    "  --extract-audio <id>",
    "  --play-audio <id>",
        "  --list-animations",
        "  --show-animation <name>",
        "  --with-audio",
        "  --overlay",
        "  --x <value>",
    "  --y <value>",
    "  --list-states",
    "  --show-state <name>",
    "  --show-first-image",
    "  --say <text>",
    "  --balloon-debug",
    "  --save-animation-gif <name>",
    "  --fps <value>",
    "  --interactive",
    "",
    "Notes:",
    "  Normal mode windows are resizable.",
    "  Overlay mode supports mouse drag.",
    "  Click on Clippy to trigger a reaction.",
    "  Right click opens a small context menu.",
]

def _print_interactive_help(topic=None):
    lines = INTERACTIVE_HELP_TOPICS.get(topic.casefold() if isinstance(topic, str) else topic)
    if lines is None:
        print(f"Unknown help topic: {topic}")
        print("Type 'help' to see available commands.")
        return
    print("\n".join(lines))

def _build_cli_help_epilog():
    return "\n".join(CLI_OPTION_LINES)

def _resolve_acs_path(character=None):
    script_dir = Path(__file__).resolve().parent
    if character is None:
        candidates = [script_dir / "CLIPPIT.ACS", script_dir / "clippit.acs"]
    else:
        path = Path(character)
        if path.suffix.lower() != ".acs":
            path = path.with_suffix(".acs")
        if path.is_absolute():
            candidates = [path]
        else:
            candidates = [Path.cwd() / path, script_dir / path]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Parse Microsoft Agent ACS files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_build_cli_help_epilog(),
    )
    parser.add_argument(
        "acs_file",
        nargs="?",
        help="Path to an ACS file. Defaults to CLIPPIT.ACS next to this script.",
    )
    parser.add_argument(
        "--test-sack",
        action="store_true",
        help="Run the built-in SACK decompression smoke test and exit.",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="Show detailed ACS character information and exit.",
    )
    parser.add_argument(
        "--list-audio",
        action="store_true",
        help="List audio clips in the ACS file and exit.",
    )
    parser.add_argument(
        "--audio-usage",
        action="store_true",
        help="Show which animations and frames use audio clips.",
    )
    parser.add_argument(
        "--audio-info",
        action="store_true",
        help="Show detailed technical information for each audio clip.",
    )
    parser.add_argument(
        "--balloon-info",
        action="store_true",
        help="Show the ACS balloon style parameters.",
    )
    parser.add_argument(
        "--timeline",
        metavar="NAME",
        help="Show a frame-by-frame timeline for an animation.",
    )
    parser.add_argument(
        "--analyze-animation",
        metavar="NAME",
        help="Show a structured summary of an animation.",
    )
    parser.add_argument(
        "--play-synced",
        metavar="NAME",
        help="Play an animation with synchronized audio.",
    )
    parser.add_argument(
        "--extract-audio",
        metavar="ID",
        help="Export an audio clip to .wav or .bin and exit.",
    )
    parser.add_argument(
        "--play-audio",
        metavar="ID",
        help="Play an audio clip and exit.",
    )
    parser.add_argument(
        "--list-animations",
        action="store_true",
        help="List the animations available in the ACS file and exit.",
    )
    parser.add_argument(
        "--list-states",
        action="store_true",
        help="List the states available in the ACS file and exit.",
    )
    parser.add_argument(
        "--show-animation",
        metavar="NAME",
        help="Display an animation by name in a loop.",
    )
    parser.add_argument(
        "--say",
        metavar="TEXT",
        help="Show dialogue text in a balloon.",
    )
    parser.add_argument(
        "--balloon-debug",
        action="store_true",
        help="Print balloon layout metrics while rendering.",
    )
    parser.add_argument(
        "--overlay",
        action="store_true",
        help="Render the character as a transparent desktop overlay.",
    )
    parser.add_argument(
        "--x",
        type=int,
        help="X position for the overlay window.",
    )
    parser.add_argument(
        "--y",
        type=int,
        help="Y position for the overlay window.",
    )
    parser.add_argument(
        "--with-audio",
        action="store_true",
        help="Play frame audio while showing an animation or state.",
    )
    parser.add_argument(
        "--show-state",
        metavar="NAME",
        help="Display a state by name in a loop.",
    )
    parser.add_argument(
        "--save-animation-gif",
        metavar="NAME",
        help="Export an animation to a GIF file named after the animation.",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=10.0,
        help="Playback speed for animations in frames per second (default: 10).",
    )
    parser.add_argument(
        "--show-first-image",
        action="store_true",
        help="Render the first image from the ACS file, save it as PNG, and display it.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Load the ACS file once and expose a small interactive command loop.",
    )
    parser.add_argument(
        "--debug-service",
        "--debug-fsm",
        action="store_true",
        help="Enable sparse service/FSM debug tracing in interactive mode.",
    )
    args = parser.parse_args(argv)
    global BALLOON_DEBUG
    BALLOON_DEBUG = bool(args.balloon_debug)
    overlay_mode = bool(args.overlay or args.x is not None or args.y is not None)

    if args.interactive:
        try:
            interactive_shell(
                args.acs_file,
                overlay=overlay_mode,
                x=args.x,
                y=args.y,
                debug_service=bool(args.debug_service),
            )
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.test_sack:
        return 0 if test_sack() else 1

    if args.info:
        try:
            info(args.acs_file or str(_resolve_acs_path()))
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.list_audio:
        try:
            list_audio(args.acs_file or str(_resolve_acs_path()))
        except (OSError, ValueError, TypeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.audio_usage:
        try:
            audio_usage(args.acs_file or str(_resolve_acs_path()))
        except (OSError, ValueError, TypeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.audio_info:
        try:
            audio_info(args.acs_file or str(_resolve_acs_path()))
        except (OSError, ValueError, TypeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.balloon_info:
        try:
            balloon_info(args.acs_file or str(_resolve_acs_path()))
        except (OSError, ValueError, TypeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.timeline:
        try:
            timeline(args.acs_file or str(_resolve_acs_path()), args.timeline)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.analyze_animation:
        try:
            analyze_animation(args.acs_file or str(_resolve_acs_path()), args.analyze_animation)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.play_synced:
        acs_path = args.acs_file or str(_resolve_acs_path())
        try:
            play_synced(acs_path, args.play_synced, fps=args.fps, say_text=args.say, overlay=overlay_mode, x=args.x, y=args.y)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.extract_audio is not None:
        try:
            output = extract_audio(args.acs_file or str(_resolve_acs_path()), int(args.extract_audio))
        except (OSError, ValueError, TypeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.play_audio is not None:
        try:
            play_object = play_audio(args.acs_file or str(_resolve_acs_path()), int(args.play_audio))
            if play_object is not None and hasattr(play_object, "wait_done"):
                play_object.wait_done()
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.list_animations:
        try:
            list_animations(args.acs_file or str(_resolve_acs_path()))
        except (OSError, ValueError, TypeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.list_states:
        try:
            list_states(args.acs_file or str(_resolve_acs_path()))
        except (OSError, ValueError, TypeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.show_animation:
        acs_path = args.acs_file or str(_resolve_acs_path())
        try:
            show_animation(acs_path, args.show_animation, fps=args.fps, with_audio=args.with_audio, say_text=args.say, overlay=overlay_mode, x=args.x, y=args.y)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.show_state:
        acs_path = args.acs_file or str(_resolve_acs_path())
        try:
            show_state(acs_path, args.show_state, fps=args.fps, with_audio=args.with_audio, say_text=args.say, overlay=overlay_mode, x=args.x, y=args.y)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.save_animation_gif:
        acs_path = args.acs_file or str(_resolve_acs_path())
        try:
            output = save_animation_gif(acs_path, args.save_animation_gif, fps=args.fps)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"Saved animation GIF to {output}")
        return 0

    if args.say:
        acs_path = args.acs_file or str(_resolve_acs_path())
        try:
            show_balloon(acs_path, args.say, overlay=overlay_mode, x=args.x, y=args.y)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.show_first_image:
        acs_path = args.acs_file or str(_resolve_acs_path())
        try:
            if overlay_mode:
                show_balloon(acs_path, "", overlay=True, x=args.x, y=args.y)
            else:
                show_first_image(acs_path)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if overlay_mode:
        acs_path = args.acs_file or str(_resolve_acs_path())
        try:
            show_balloon(acs_path, "", overlay=True, x=args.x, y=args.y)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.acs_file is None:
        args.acs_file = str(_resolve_acs_path())

    try:
        character = AgentCharacter(_resolve_acs_path(args.acs_file))
    except (OSError, ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parsed = character.data
    print(
        f"Loaded {args.acs_file} | signature=0x{parsed.signature:08x} | "
        f"animations={len(parsed.acsanimationinfo)} | images={len(parsed.acsimageinfo)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
