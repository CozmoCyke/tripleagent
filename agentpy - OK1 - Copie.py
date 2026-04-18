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

from collections import namedtuple

BALLOON_DEBUG = False

class AgentCharacter(object):
    def __init__(self, filename):
        with open(filename, "rb") as f:
            parser = ACSParser(f.read())
        self.data = parser.parse()

class ACSParser(object):
    # Data structures
    acsheader = namedtuple("acsheader", ["signature", "acscharacterinfo",
                                                        "acsanimationinfo", "acsimageinfo", "acsaudioinfo", "SIZE"])
    acslocator = namedtuple("acslocator", ["offset", "data_size", "SIZE"])
    
    acscharacterinfo = namedtuple("acscharacterinfo", ["minor_version",
                                                                                    "major_version", "localizedinfo", "guid", "width",
                                                                                    "height", "transparent_color_index", "flags",
                                                                                    "animation_set_major_version",
                                                                                    "animation_set_minior_version", "voiceinfo",
                                                                                    "ballooninfo", "color_table", "tray_icon_flag",
                                                                                    "tray_icon", "stateinfo", "SIZE"])
    charflags = namedtuple("charflags", ["voice_enabled", "balloon_enabled",
                                                        "size_to_text", "auto_hide", "auto_pace", "std_anim_set",
                                                        "flags", "SIZE"])
    localizedinfo_locale = namedtuple("localeizedinfo_locale", ["lang_id",
                                                                                                    "name", "desc", "extra", "SIZE"])
    voiceinfo = namedtuple("voiceinfo", ["tts_engine_id", "tts_mode_id",
                                                        "speed", "pitch", "extra_data_flag", "lang_id", "lang_dialect",
                                                        "gender", "age", "style", "SIZE"])
    ballooninfo = namedtuple("ballooninfo", ["num_lines", "chars_per_line",
                                                                "fgcolor", "bgcolor", "border_color", "font_name",
                                                                "font_height", "font_weight", "italic_flag", "unknown",
                                                                "SIZE"])
    trayicon = namedtuple("trayicon", ["mono_size", "mono_dib",
                                                                                            "color_size", "color_dib", "SIZE"])
    state = namedtuple("state", ["name", "animations", "SIZE"])
    
    acsanimationinfo = namedtuple("acsanimationinfo", ["name", "animation_data",
                                                                                    "SIZE"])
    acsanimationinfo_data = namedtuple("acsanimationinfo_data", ["name",
                                                                                                        "transition_type", "return_animation", "frames",
                                                                                                        "SIZE"])
    acsframeinfo = namedtuple("acsframeinfo", ["images", "audio_index",
                                                                    "frame_duration_csecs", "exit_to_frame_index",
                                                                    "frame_branches", "mouth_overlays", "SIZE"])
    acsframeimage = namedtuple("acsframeimage", ["image_index", "x_offset",
                                                                        "y_offset", "SIZE"])
    branchinfo = namedtuple("branchinfo", ["jump_to_frame_index",
                                                            "probability_percent", "SIZE"])
    acsoverlayinfo = namedtuple("acsoverlayinfo", ["overlay_type",
                                                                            "replace_top_image_of_frame", "image_index", "unknown",
                                                                            "region_data_flag", "x_offset", "y_offset", "width",
                                                                            "height", "region_data", "SIZE"])
    
    acsimageinfo = namedtuple("acsimageinfo", ["image_data", "checksum_maybe",
                                                                    "SIZE"])
    acsimageinfo_data = namedtuple("acsimageinfo_data", ["unknown", "width",
                                                                                        "height", "image_compressed", "image_data",
                                                                                        "rgndata_size_compressed", "rgndata_size_uncompressed",
                                                                                        "rgndata_size", "rgndata", "SIZE"])
    
    acsaudioinfo = namedtuple("acsaudioinfo", ["audio_data", "checksum_maybe",
                                                                    "SIZE"])
    
    rect = namedtuple("rect", ["upper_left", "lower_right", "SIZE"])
    rgbquad = namedtuple("rgbquad", ["red", "green", "blue", "reserved", "int",
                                                "hex", "SIZE"])
    rgndata = namedtuple("rgndata", ["header_size", "region_type", "num_rects",
                                                "buffer_size", "bounds", "rects", "SIZE"])
    
    # "Public" methods
    def __init__(self, data):
        if isinstance(data, str):
            raise TypeError("ACSParser requires bytes-like data, not str")
        self.data = bytes(data)
        self.size = len(self.data)
    def parse(self):
        return self.parse_acsheader()
    # Helper methods
    def check(self, offset, size):
        if self.size < (offset + size):
            raise ValueError("data must be at least %d bytes long" % (offset + size))
    @classmethod
    def decompress_sack(cls, data, dst_size):
        """Decompress data encoded in the proprietary SACK format."""
        # SACK = Shitty Agent Compression Klusterfuck
        class _LittleEndianBitStream:
            def __init__(self, raw):
                self.raw = raw
                self.size = len(raw) * 8

            def _check_range(self, start, length):
                if start < 0 or length < 0 or (start + length) > self.size:
                    raise ValueError("malformed compressed data")

            def bit(self, index):
                self._check_range(index, 1)
                return bool((self.raw[index // 8] >> (index % 8)) & 1)

            def bits_to_int(self, start, length):
                self._check_range(start, length)
                value = 0
                for bit_offset in range(length):
                    if self.bit(start + bit_offset):
                        value |= 1 << bit_offset
                return value

        data = bytes(data)
        if len(data) < 7 or data[:1] != b"\x00" or data[-6:] != b"\xff" * 6:
            raise ValueError("malformed compressed data")
        src = _LittleEndianBitStream(data)
        dst = bytearray(dst_size)
        src_n = 8
        dst_ip = 0
        while src_n < src.size:
            #print "==========================================================="
            #print repr(dst[:dst_ip])
            #print "src_n = "+str(src_n)+" bits; dst_ip = "+str(dst_ip)+" bytes"
            if not src.bit(src_n):
                # Decompressed byte follows
                #print "uncompressed,", src[src_n+1:src_n+9], to_int(src[src_n+1:src_n+9])
                dst[dst_ip] = src.bits_to_int(src_n + 1, 8)
                src_n += 9; dst_ip += 1
                continue
            # Compressed data follows
            #print "compressed,",
            src_n += 1
            n_bytes = 2
            # Get number of bits in next number
            n_1_bits = 0
            while n_1_bits < 3:
                if not src.bit(src_n):
                    src_n += 1
                    break
                src_n += 1
                n_1_bits += 1
            next_bit_count = (6,9,12,20)[n_1_bits]
            #print "next_bit_count="+str(next_bit_count),
            # Get read offset from insertion point in destination buffer
            dst_ip_offset = src.bits_to_int(src_n, next_bit_count)
            src_n += next_bit_count
            # Detect end of stream
            if next_bit_count == 20:
                #print dst_ip_offset,
                if dst_ip_offset == 0xFFFFF:
                    #print
                    break
                n_bytes += 1
            dst_ip_offset += (1,65,577,4673)[n_1_bits]
            #print "dst_ip_offset="+str(dst_ip_offset)
            # Get number of bytes to copy
            n_1_bits = 0
            while n_1_bits < 12:
                src_n += 1
                if not src.bit(src_n - 1): break
                n_1_bits += 1
            #print "n_1_bits="+str(n_1_bits),
            if n_1_bits == 12: raise ValueError("malformed data at " + hex(src_n-12))
            if n_1_bits:
                n_bytes += src.bits_to_int(src_n - n_1_bits - 1, n_1_bits)
                n_bytes += src.bits_to_int(src_n, n_1_bits)
            src_n += n_1_bits
            #print "n_bytes="+str(n_bytes)
            # Now, COPY the damn fuckers!
            n_copied = 0
            while n_copied < n_bytes:
                dst[dst_ip] = dst[dst_ip-dst_ip_offset]
                dst_ip += 1
                n_copied += 1
        #print "==========================================================="
        # I finally did it!  Praise Ballmer!
        return dst
    # ACS-specific types
    def parse_acsheader(self):
        sig = self.parse_ulong(0)
        if sig != 0xabcdabc3:
            raise ValueError("not a valid Agent character file")
        return self.acsheader(
            sig,
            self.parse_acscharacterinfo(*self.parse_acslocator(4)),
            self.parse_acsanimationinfo_list(*self.parse_acslocator(12)),
            self.parse_acsimageinfo_list(*self.parse_acslocator(20)),
            self.parse_acsaudioinfo_list(*self.parse_acslocator(28)),
        36)
    def parse_acslocator(self, offset):
        offset_value, data_size = struct.unpack("<LL", self.data[offset:offset+8])
        return self.acslocator(offset_value, data_size, 8)
    # ACS Character Info (metadata)
    def parse_acscharacterinfo(self, offset, size, *_):
        self.check(offset, size)
        start = offset
        minor_version = self.parse_ushort(offset)
        major_version = self.parse_ushort(offset + 2)
        offset += 4
        localizedinfo_location = self.parse_acslocator(offset)
        localizedinfo = self.parse_localizedinfo(localizedinfo_location)
        offset += localizedinfo_location.SIZE
        guid = self.parse_guid(offset)
        offset += 16
        width = self.parse_ushort(offset)
        height = self.parse_ushort(offset + 2)
        offset += 4
        transparent_color_index = self.parse_byte(offset)
        offset += 1
        flags = self.parse_charflags(offset)
        offset += 4
        anim_set_major = self.parse_ushort(offset)
        anim_set_minor = self.parse_ushort(offset + 2)
        offset += 4
        if flags.voice_enabled:
            voiceinfo = self.parse_voiceinfo(offset)
            offset += voiceinfo.SIZE
        else:
            voiceinfo = None
        if flags.balloon_enabled:
            ballooninfo = self.parse_ballooninfo(offset)
            offset += ballooninfo.SIZE
        else:
            ballooninfo = None
        color_table = self.parse_color_table(offset)
        offset += color_table.SIZE
        tray_icon_flag = self.parse_byte(offset)
        offset += 1
        if tray_icon_flag:
            tray_icon = self.parse_trayicon(offset)
            offset += tray_icon.SIZE
        else:
            tray_icon = None
        stateinfo = self.parse_stateinfo(offset)
        offset += stateinfo.SIZE
        if offset - start + localizedinfo_location.data_size != size:
            raise ValueError("malformed acscharacterinfo")
        return self.acscharacterinfo(
            minor_version, major_version, localizedinfo, guid, width, height,
            transparent_color_index, flags, anim_set_major, anim_set_minor, voiceinfo,
            ballooninfo, color_table, tray_icon_flag, tray_icon, stateinfo,
        offset - start)
    def parse_localizedinfo(self, acslocator):
        offset = acslocator.offset
        size = acslocator.data_size
        l = self.parse_list(offset, self.parse_ushort, 2,
                            self.parse_localizedinfo_locale, lambda i: i.SIZE)
        if l.SIZE != size:
            raise ValueError("malformed locale at " + hex(offset))
        ret = ACSDict([(i.lang_id, i) for i in l], l.SIZE)
        return ret
    def parse_localizedinfo_locale(self, offset):
        start = offset
        lang_id = self.parse_langid(offset)
        offset += 2
        name = self.parse_string(offset)
        offset += name.SIZE
        desc = self.parse_string(offset)
        offset += desc.SIZE
        extra = self.parse_string(offset)
        offset += extra.SIZE
        return self.localizedinfo_locale(lang_id, name, desc, extra, offset - start)
    def parse_charflags(self, offset):
        flags = self.parse_ulong(offset)
        # Seriously, Microsoft, WTF?
        voice_disabled = bool(flags >> 4 & 1)
        voice_enabled = bool(flags >> 5 & 1)
        if voice_disabled == voice_enabled:
            raise ValueError("Voice Output is both enabled and disabled")
        balloon_disabled = bool(flags >> 8 & 1)
        balloon_enabled = bool(flags >> 9 & 1)
        if balloon_disabled == balloon_enabled:
            raise ValueError("Word Balloon is both enabled and disabled")
        size_to_text = bool(flags >> 16 & 1)
        auto_hide = not bool(flags >> 17 & 1)
        auto_pace = not bool(flags >> 18 & 1)
        std_anim_set = bool(flags >> 20 & 1)
        return self.charflags(
            voice_enabled, balloon_enabled, size_to_text, auto_hide, auto_pace,
            std_anim_set, flags,
        32)
    def parse_voiceinfo(self, offset):
        start = offset
        tts_engine_id = self.parse_guid(offset)
        tts_mode_id = self.parse_guid(offset + 16)
        offset += 32
        speed = self.parse_ulong(offset)
        pitch = self.parse_ushort(offset + 4)
        extra_data_flag = self.parse_byte(offset + 6)
        offset += 7
        if extra_data_flag & 1:
            lang_id = self.parse_langid(offset)
            lang_dialect = self.parse_string(offset + 2)
            offset += lang_dialect.SIZE + 2
            gender = self.parse_ushort(offset)
            age = self.parse_ushort(offset + 2)
            style = self.parse_string(offset + 4)
            offset += style.SIZE + 4
        else:
            lang_id = lang_dialect = gender = age = style = None
        return self.voiceinfo(
            tts_engine_id, tts_mode_id, speed, pitch, extra_data_flag,
            lang_id, lang_dialect, gender, age, style,
        offset - start)
    def parse_ballooninfo(self, offset):
        start = offset
        num_lines = self.parse_byte(offset)
        chars_per_line = self.parse_byte(offset + 1)
        offset += 2
        fgcolor = self.parse_rgbquad(offset)
        offset += 4
        bgcolor = self.parse_rgbquad(offset)
        offset += 4
        border_color = self.parse_rgbquad(offset)
        offset += 4
        font_name = self.parse_string(offset)
        offset += font_name.SIZE
        font_height = self.parse_long(offset)
        font_weight = self.parse_long(offset + 4)
        italic_flag = self.parse_byte(offset + 8)
        unknown = self.parse_byte(offset + 9)
        offset += 10
        return self.ballooninfo(
            num_lines, chars_per_line, fgcolor, bgcolor, border_color, font_name,
            font_height, font_weight, italic_flag, unknown,
        offset - start)
    def parse_color_table(self, offset):
        return self.parse_list(offset, self.parse_ulong, 4,
                                                                                                    self.parse_rgbquad, 4)
    def parse_trayicon(self, offset):
        start = offset
        mono_size = self.parse_ulong(offset)
        offset += 4
        mono_dib = self.data[offset:offset+mono_size]
        offset += mono_size
        color_size = self.parse_ulong(offset)
        offset += 4
        color_dib = self.data[offset:offset+color_size]
        offset += color_size
        return self.trayicon(
            mono_size, mono_dib, color_size, color_dib,
        offset - start)
    def parse_stateinfo(self, offset):
        l = self.parse_list(offset, self.parse_ushort, 2, self.parse_state,
                                                                                        lambda i: i.SIZE)
        return ACSDict([(i.name, i) for i in l], l.SIZE)
    def parse_state(self, offset):
        start = offset
        name = self.parse_string(offset)
        offset += name.SIZE
        animations = self.parse_list(offset, self.parse_ushort, 2, self.parse_string,
                                                                                                                            lambda i: i.SIZE)
        offset += animations.SIZE
        return self.state(name, animations, offset - start)
    # ACS Animation Info
    def parse_acsanimationinfo_list(self, offset, size, *_):
        self.check(offset, size)
        ret = self.parse_list(offset, self.parse_ulong, 4,
                                                                                                self.parse_acsanimationinfo, lambda i: i.SIZE)
        if ret.SIZE != size:
            raise ValueError("malformed acsanimationinfo list")
        return ret
    def parse_acsanimationinfo(self, offset):
        start = offset
        name = self.parse_string(offset)
        offset += name.SIZE
        animdata_location = self.parse_acslocator(offset)
        offset += animdata_location.SIZE
        animdata = self.parse_acsanimationinfo_data(*animdata_location)
        return self.acsanimationinfo(name, animdata, offset - start)
    def parse_acsanimationinfo_data(self, offset, size, *_):
        self.check(offset, size)
        start = offset
        name = self.parse_string(offset)
        offset += name.SIZE
        transition_type = {0: "use_return_animation",
                                                                                    1: "use_exit_branches",
                                                                                    2: None}.get(self.parse_byte(offset), None)
        offset += 1
        return_animation = self.parse_string(offset)
        offset += return_animation.SIZE
        frames = self.parse_list(offset, self.parse_ushort, 2,
                                                                                                            self.parse_acsframeinfo, lambda i: i.SIZE)
        offset += frames.SIZE
        if offset - start != size:
            raise ValueError("malformed animation data")
        return self.acsanimationinfo_data(
            name, transition_type, return_animation, frames,
        size)
    def parse_acsframeinfo(self, offset):
        start = offset
        images = self.parse_list(offset, self.parse_ushort, 2,
                                                                                                            self.parse_acsframeimage, lambda i: i.SIZE)
        offset += images.SIZE
        audio_index = self.parse_ushort(offset)
        frame_duration_csecs = self.parse_ushort(offset + 2)
        exit_to_frame_index = self.parse_short(offset + 4)
        offset += 6
        frame_branches = self.parse_list(offset, self.parse_byte, 1,
                                                                                                                                            self.parse_branchinfo, lambda i: i.SIZE)
        offset += frame_branches.SIZE
        mouth_overlays = self.parse_list(offset, self.parse_byte, 1,
                                                                                                                                            self.parse_acsoverlayinfo,lambda i:i.SIZE)
        offset += mouth_overlays.SIZE
        return self.acsframeinfo(
            images, audio_index, frame_duration_csecs, exit_to_frame_index,
            frame_branches, mouth_overlays,
        offset - start)
    def parse_acsframeimage(self, offset):
        # TODO: image_index should eventually be the actual image data maybe?
        image_index = self.parse_ulong(offset)
        x_offset = self.parse_short(offset + 4)
        y_offset = self.parse_short(offset + 6)
        return self.acsframeimage(image_index, x_offset, y_offset, 8)
    def parse_branchinfo(self, offset):
        jump_to_frame_index = self.parse_ushort(offset)
        probability_percent = self.parse_ushort(offset + 2)
        return self.branchinfo(jump_to_frame_index, probability_percent, 4)
    def parse_acsoverlayinfo(self, offset):
        start = offset
        overlay_type = {0: "mouth_closed", 1: "mouth_wide_open_1",
                                                                        2: "mouth_wide_open_2", 3: "mouth_wide_open_3",
                                                                        4: "mouth_wide_open_4", 5: "mouth_medium",
                                                                        6: "mouth_narrow"}.get(self.parse_byte(offset), None)
        replace_top_image_of_frame = bool(self.parse_byte(offset + 1))
        offset += 2
        image_index = self.parse_ushort(offset)
        offset += 2
        unknown = self.parse_byte(offset)
        region_data_flag = bool(self.parse_byte(offset + 1))
        offset += 2
        x_offset = self.parse_short(offset)
        y_offset = self.parse_short(offset + 2)
        width = self.parse_ushort(offset + 4) * 2
        height = self.parse_ushort(offset + 6) * 2
        offset += 8
        if region_data_flag:
            region_data_size = self.parse_ulong(offset)
            offset += 4
            region_data = self.parse_rgndata(offset, region_data_size)
            offset += region_data.SIZE
        else:
            region_data = None
        return self.acsoverlayinfo(
            overlay_type, replace_top_image_of_frame, image_index, unknown,
            region_data_flag, x_offset, y_offset, width, height, region_data,
        offset - start)
    # ACS Image Info List
    def parse_acsimageinfo_list(self, offset, size, *_):
        self.check(offset, size)
        ret = self.parse_list(offset, self.parse_ulong, 4,
                                                                                                self.parse_acsimageinfo, lambda i: i.SIZE)
        if ret.SIZE != size:
            raise ValueError("malformed acsimageinfo list")
        return ret
    def parse_acsimageinfo(self, offset):
        start = offset
        location = self.parse_acslocator(offset)
        offset += location.SIZE
        checksum_maybe = self.parse_ulong(offset)
        offset += 4
        image_data = self.parse_acsimageinfo_data(*location)
        return self.acsimageinfo(image_data, checksum_maybe, offset - start)
    def parse_acsimageinfo_data(self, offset, size, *_):
        self.check(offset, size)
        start = offset
        unknown = self.parse_byte(offset)
        offset += 1
        width = self.parse_ushort(offset)
        height = self.parse_ushort(offset + 2)
        offset += 4
        image_compressed = bool(self.parse_byte(offset))
        offset += 1
        image_data = self.parse_datablock(offset)
        offset += image_data.SIZE
        if image_compressed:
            image_data = self.decompress_sack(image_data, ((width+3) & 0xFC) * height)
        rgndata_size_compressed = self.parse_ulong(offset)
        rgndata_size_uncompressed = self.parse_ulong(offset + 4)
        offset += 8
        rgndata_compressed = bool(rgndata_size_compressed)
        rgndata_size = rgndata_size_compressed or rgndata_size_uncompressed
        rgndata = self.data[offset:offset+rgndata_size]
        offset += rgndata_size
        if rgndata_compressed:
            rgndata_size = rgndata_size_uncompressed
            rgndata = self.decompress_sack(rgndata, rgndata_size)
        rgndata = ACSParser(rgndata).parse_rgndata(0, rgndata_size)
        if offset - start != size:
            raise ValueError("malformed acsimageinfo_data")
        return self.acsimageinfo_data(
            unknown, width, height, image_compressed, image_data,
            rgndata_size_compressed, rgndata_size_uncompressed, rgndata_size, rgndata,
        size)
    # ACS Audio Info List
    def parse_acsaudioinfo_list(self, offset, size, *_):
        self.check(offset, size)
        ret = self.parse_list(offset, self.parse_ulong, 4,
                                                                                                self.parse_acsaudioinfo, lambda i: i.SIZE)
        if ret.SIZE != size:
            raise ValueError("malformed acsaudioinfo list")
        return ret
    def parse_acsaudioinfo(self, offset):
        start = offset
        audio_data_location = self.parse_acslocator(offset)
        offset += audio_data_location.SIZE
        checksum_maybe = self.parse_ulong(offset)
        offset += 4
        audio_data = self.parse_audio_data(*audio_data_location)
        return self.acsaudioinfo(audio_data, checksum_maybe, offset - start)
    def parse_audio_data(self, offset, size, *_):
        self.check(offset, size)
        audio_data = self.data[offset:offset+size]
        return audio_data
    # Primitives
    def parse_byte(self, offset):
        return struct.unpack("<B", self.data[offset:offset+1])[0]
    def parse_long(self, offset):
        return struct.unpack("<l", self.data[offset:offset+4])[0]
    def parse_ulong(self, offset):
        return struct.unpack("<L", self.data[offset:offset+4])[0]
    def parse_short(self, offset):
        return struct.unpack("<h", self.data[offset:offset+2])[0]
    def parse_ushort(self, offset):
        return struct.unpack("<H", self.data[offset:offset+2])[0]
    def parse_wchar(self, offset):
        return struct.unpack("<h", self.data[offset:offset+2])[0]
    # Other common types
    def parse_datablock(self, offset):
        # (header--size in bytes) + 4 bytes in header
        size = self.parse_ulong(offset) + 4
        offset += 4
        return ACSDataBlock(self.data[offset:offset+size-4], size)
    def parse_guid(self, offset):
        return uuid.UUID(bytes_le=self.data[offset:offset+16])
    def parse_langid(self, offset):
        return self.parse_ushort(offset)
    def parse_rect(self, offset):
        top_left_x = self.parse_long(offset)
        top_left_y = self.parse_long(offset + 4)
        bottom_right_x = self.parse_long(offset + 8)
        bottom_right_y = self.parse_long(offset + 12)
        return self.rect(
            (top_left_x, top_left_y), (bottom_right_x, bottom_right_y), 16
        )
    def parse_rgbquad(self, offset):
        red = self.parse_byte(offset)
        green = self.parse_byte(offset + 1)
        blue = self.parse_byte(offset + 2)
        reserved = self.parse_byte(offset + 3)
        int_ = (red * 0x10000) + (green * 0x100) + blue
        hex_ = hex(red)[2:].zfill(2)+hex(green)[2:].zfill(2)+hex(blue)[2:].zfill(2)
        return self.rgbquad(red, green, blue, reserved, int_, hex_, 4)
    def parse_rgndata(self, offset, size):
        self.check(offset, size)
        start = offset
        header_size = self.parse_ulong(offset)
        region_type = self.parse_ulong(offset + 4)
        num_rects = self.parse_ulong(offset + 8)
        buffer_size = self.parse_ulong(offset + 12)
        bounds = self.parse_rect(offset + 16)
        offset += 32
        rects = self.parse_list(offset, num_rects, 0, self.parse_rect,
                                                                                                        lambda i: i.SIZE)
        offset += rects.SIZE
        if offset - start != size:
            raise ValueError("malformed rgndata")
        return self.rgndata(
            header_size, region_type, num_rects, buffer_size, bounds, rects, size
        )
    def parse_list(self, offset, count, count_size, struct_parser, struct_size):
        start = offset
        size = count(offset) if callable(count) else count
        if callable(count_size): count_size = count_size(size)
        offset += count_size
        ret = []
        if not callable(struct_size):
            self.check(offset, struct_size * size)
            struct_size_ = struct_size
            struct_size = lambda s: struct_size_
        n = 0
        while n < size:
            st = struct_parser(offset)
            ret.append(st)
            offset += struct_size(st)
            n += 1
        return ACSList(ret, offset - start)
    def parse_string(self, offset):
        num_chars = self.parse_ulong(offset)
        terminator_len = 2 if num_chars else 0
        offset += 4
        size = (num_chars * 2) + terminator_len + 4
        return ACSString(self.data[offset:offset+size-terminator_len-4], size)

class ACSDataBlock(bytes):
    def __new__(cls, data, size):
        return super(ACSDataBlock, cls).__new__(cls, data)
    def __init__(self, data, size):
        self.SIZE = size

class ACSDict(dict):
    def __new__(cls, iterable, size):
        return super(ACSDict, cls).__new__(cls, iterable)
    def __init__(self, iterable, size):
        self.update(iterable)
        self.SIZE = size

class ACSList(list):
    def __new__(cls, iterable, size):
        return super(ACSList, cls).__new__(cls, iterable)
    def __init__(self, iterable, size):
        self += iterable
        self.SIZE = size

class ACSString(str):
    def __new__(cls, data, size):
        return super(ACSString, cls).__new__(cls, data, "utf_16_le", "strict")
    def __init__(self, data, size):
        self.SIZE = size

def test(character="clippit"):
    path = _resolve_acs_path(character)
    return AgentCharacter(path)

def testd(character="clippit"):
    path = _resolve_acs_path(character)
    with open(path, "rb") as f:
        return f.read()

def test_sack():
    # Sample data taken from Remy Lebeau's MS Agent Character Data Specification
    # at http://j.mp/msagentcharspec (mirror: http://j.mp/msagentcharspecmirror)
    compressed = bytearray(b"\x00@\x00\x04\x10\xd0\x90\x80B\xed\x98\x01\xb7\xff"
                                                                                                b"\xff\xff\xff\xff\xff")
    expected_result = bytearray(b" \x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00"
                                                                                                                    b"\xa8\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
                                                                                                                    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00")
    actual_result = ACSParser.decompress_sack(compressed, len(expected_result))
    if expected_result == actual_result:
        print("it works")
        return True
    print( "doesn't work")
    print( "expected_result = " + repr(expected_result))
    print("actual_result = " + repr(actual_result))
    return False

def _acs_palette_to_bytes(color_table):
    palette = bytearray()
    for color in color_table:
        palette.extend((color.red, color.green, color.blue))
    palette.extend(b"\x00" * (256 * 3 - len(palette)))
    return bytes(palette[:256 * 3])

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
        self.sprite_cache = {}
        self.animation_cache = {}
        self.state_cache = {}
        self.timeline_cache = {}
        self.balloon_cache = {}
        self.audio_cache = {}
        self._image_module = None
        self._sprite_cache_built = False
        self.sprite_cache_seconds = None

        print(f"ACS parse: {self.parse_seconds:.3f}s")

    def _get_image_module(self):
        if self._image_module is None:
            self._image_module = _require_pillow()
        return self._image_module

    def _ensure_sprite_cache(self):
        if self._sprite_cache_built:
            return
        start = time.perf_counter()
        image_module = self._get_image_module()
        transparent_color_index = self.character.transparent_color_index
        for index, image_info in enumerate(self.agent.data.acsimageinfo):
            sprite_info = image_info.image_data
            if sprite_info.width <= 0 or sprite_info.height <= 0:
                raise ValueError(f"invalid sprite dimensions at image index {index}")
            sprite = image_module.frombytes("P", (sprite_info.width, sprite_info.height), bytes(sprite_info.image_data))
            sprite.putpalette(self.palette_bytes)
            if 0 <= transparent_color_index < 256:
                sprite.info["transparency"] = transparent_color_index
            # ACS bitmaps are stored bottom-up like Windows DIBs.
            sprite = sprite.transpose(image_module.FLIP_TOP_BOTTOM)
            self.sprite_cache[index] = sprite.convert("RGBA")
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
        return "\n".join([
            "Balloon info",
            "-------------",
            "Balloon: yes",
            f"num_lines: {balloon.num_lines}",
            f"chars_per_line: {balloon.chars_per_line}",
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

    def _wrap_balloon_text(self, text, draw, font, max_width_px, max_lines):
        def measure(line):
            bbox = self._measure_text_bbox(draw, line, font)
            return bbox[2] - bbox[0]

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

    def balloon_overlay(self, text, base_size=None):
        from PIL import Image, ImageDraw
        base_width, base_height = base_size or (self.character.width, self.character.height)
        cache_key = (str(text), base_width, base_height)
        cached = self.balloon_cache.get(cache_key)
        if cached is not None:
            return cached.copy()
        balloon = self.character.ballooninfo
        overlay = Image.new("RGBA", (base_width, base_height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        font = self._load_balloon_font()
        font_height = abs(int(getattr(balloon, "font_height", 14) or 14)) if balloon else 14
        spacing = max(1, int(round(font_height * 0.15)))
        balloon_lines = max(1, int(getattr(balloon, "num_lines", 3) or 3)) if balloon else 3
        chars_per_line = max(1, int(getattr(balloon, "chars_per_line", 24) or 24)) if balloon else 24
        sample_char = "M"
        try:
            sample_bbox = self._measure_text_bbox(draw, sample_char, font)
            average_char_width = max(6, sample_bbox[2] - sample_bbox[0])
        except Exception:
            average_char_width = 8
        soft_width_px = chars_per_line * average_char_width
        max_width_px = min(
            max(120, int(base_width * 0.55)),
            max(120, soft_width_px),
            base_width - 24,
        )
        lines = self._wrap_balloon_text(text, draw, font, max_width_px, balloon_lines)
        text_block = "\n".join(lines)
        bbox = self._measure_text_bbox(draw, text_block, font, spacing=spacing)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        padding_x = max(8, min(14, int(round(text_height * 0.10))))
        padding_y = max(6, min(12, int(round(text_height * 0.09))))
        tail_height = max(10, int(round(font_height * 0.9)))
        bubble_width = min(base_width - 20, max(text_width + padding_x * 2, 100))
        bubble_height = min(base_height - 20, max(text_height + padding_y * 2 + tail_height, 42))
        bubble_x = max(8, base_width - bubble_width - 12)
        bubble_y = 8
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
        try:
            draw.rounded_rectangle(
                [bubble_x, bubble_y, bubble_right, bubble_bottom],
                radius=10,
                fill=bg,
                outline=border,
                width=2,
            )
        except AttributeError:
            draw.rectangle(
                [bubble_x, bubble_y, bubble_right, bubble_bottom],
                fill=bg,
                outline=border,
                width=2,
            )
        tail_top = max(bubble_bottom - 1, 0)
        tail_x_mid = max(bubble_x + 20, min(bubble_right - 20, bubble_x + max(24, bubble_width // 5)))
        tail = [
            (tail_x_mid - 8, tail_top),
            (tail_x_mid + 8, tail_top),
            (tail_x_mid, min(base_height - 1, tail_top + tail_height)),
        ]
        draw.polygon(tail, fill=bg, outline=border)
        text_x = bubble_x + padding_x
        text_y = bubble_y + padding_y
        draw.multiline_text((text_x, text_y), text_block, fill=fg, font=font, spacing=spacing)
        if BALLOON_DEBUG:
            print(f"Balloon debug: width={bubble_width} height={bubble_height} lines={len(lines)}")
        self.balloon_cache[cache_key] = overlay
        return overlay.copy()

    def compose_balloon_frame(self, frame, text):
        from PIL import Image
        if not text:
            return frame.copy()
        base = frame.convert("RGBA")
        overlay = self.balloon_overlay(text, base.size)
        return Image.alpha_composite(base, overlay)

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
        self._default_overlay_enabled = False
        self._default_overlay_x = None
        self._default_overlay_y = None
        self._status = {
            "mode": "idle",
            "animation_name": None,
            "fps": 0.0,
            "synced": False,
            "with_audio": False,
            "debug_sync": False,
            "overlay": False,
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
        }

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
            from PIL import ImageTk
        except ImportError as exc:
            self._startup_error = exc
            self._startup_error_detail = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            self._closed.set()
            return

        try:
                root = tk.Tk()
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
                    "current_audio": None,
                    "current_audio_index": None,
                    "base_frame": None,
                    "balloon_text": None,
                    "photo": None,
                    "animation_name": None,
                    "after_id": None,
                    "generation": 0,
                    "mode": "idle",
                    "window_created": False,
                }
                transparent_key = "#010203"

                def apply_window_mode():
                    if state["overlay_enabled"]:
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
                            root.attributes("-topmost", False)
                        except Exception:
                            pass
                        try:
                            root.wm_attributes("-transparentcolor", "")
                        except Exception:
                            pass

                def sync_window_geometry():
                    width = state["window_width"]
                    height = state["window_height"]
                    if width is None or height is None:
                        return
                    x = state["window_x"]
                    y = state["window_y"]
                    if not state["overlay_enabled"] and x is None and y is None:
                        publish_status()
                        return
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
                    try:
                        root.geometry(f"{int(width)}x{int(height)}+{int(x)}+{int(y)}")
                    except Exception:
                        pass
                    publish_status()

                def publish_status():
                    snapshot = {
                        "mode": state["mode"],
                        "animation_name": state["animation_name"],
                        "fps": state["fps"],
                        "synced": state["synced"],
                        "with_audio": state["with_audio"],
                        "debug_sync": state["debug_sync"],
                        "overlay": state["overlay_enabled"],
                        "player_created": True,
                        "window_created": state["window_created"],
                        "x": state["window_x"],
                        "y": state["window_y"],
                        "window_width": state["window_width"],
                        "window_height": state["window_height"],
                        "frame_index": state["current_frame_index"],
                        "frame_count": len(state["frames"]),
                        "balloon_text": state["balloon_text"],
                        "current_audio_index": state["current_audio_index"],
                    }
                    with self._status_lock:
                        self._status = snapshot

                state["window_created"] = True
                publish_status()
                apply_window_mode()
                sync_window_geometry()

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
                    balloon_text = state["balloon_text"]
                    if balloon_text:
                        display_frame = self.runtime.compose_balloon_frame(base_frame, balloon_text)
                    photo = ImageTk.PhotoImage(display_frame)
                    state["photo"] = photo
                    state["window_width"], state["window_height"] = display_frame.size
                    label.configure(image=photo, bg=transparent_key if state["overlay_enabled"] else normal_bg)
                    if state["overlay_enabled"]:
                        apply_window_mode()
                    sync_window_geometry()

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
                    publish_status()
                    state["index"] = (state["index"] + 1) % len(state["frames"])
                    next_delay_ms = entry["duration_ms"] if entry is not None else state["interval_ms"]
                    schedule_tick(next_delay_ms, generation)

                def set_animation(animation_name, frames, fps, with_audio=False, synced=False, timeline_entries=None, balloon_text=None):
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
                    state["mode"] = "sync" if synced else "animation"
                    if balloon_text is not None:
                        state["balloon_text"] = str(balloon_text)
                    root.title(f"{animation_name} - {self.runtime.character_path.name} @ {fps:g} fps")
                    publish_status()
                    tick(state["generation"])

                def say_balloon(text):
                    state["balloon_text"] = None if text is None else str(text)
                    if text is not None and state["base_frame"] is None and not state["frames"]:
                        try:
                            state["base_frame"] = self.runtime.first_image()
                        except Exception:
                            pass
                    redraw_current_frame()
                    publish_status()

                def stop_animation():
                    cancel_tick()
                    stop_current_audio()
                    state["playing"] = False
                    state["mode"] = "idle"
                    state["generation"] += 1
                    state["current_frame_index"] = None
                    publish_status()

                def set_debug_sync(enabled):
                    state["debug_sync"] = bool(enabled)
                    publish_status()

                def set_overlay_mode(enabled, x=None, y=None):
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
                    apply_window_mode()
                    if state["base_frame"] is not None:
                        redraw_current_frame()
                    else:
                        sync_window_geometry()
                    publish_status()

                def move_overlay(x, y):
                    state["window_x"] = int(x)
                    state["window_y"] = int(y)
                    sync_window_geometry()

                def close_player():
                    cancel_tick()
                    stop_current_audio()
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
                            _, animation_name, frames, fps, with_audio, synced, timeline_entries, balloon_text = command
                            set_animation(
                                animation_name,
                                frames,
                                fps,
                                with_audio=with_audio,
                                synced=synced,
                                timeline_entries=timeline_entries,
                                balloon_text=balloon_text,
                            )
                        elif kind == "say":
                            _, text = command
                            say_balloon(text)
                        elif kind == "stop":
                            stop_animation()
                        elif kind == "debug-sync":
                            _, enabled = command
                            set_debug_sync(enabled)
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
                        root.after(30, process_commands)

                root.protocol("WM_DELETE_WINDOW", close_player)
                self._ready.set()
                root.after(0, process_commands)
                root.mainloop()
        except Exception as exc:
            self._startup_error = exc
            self._startup_error_detail = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            self._closed.set()

    def play(self, animation_name, frames, fps=10.0, with_audio=False, synced=False, timeline_entries=None, balloon_text=None):
        self._start()
        self._commands.put(("set", animation_name, list(frames), float(fps), bool(with_audio), bool(synced), list(timeline_entries or []), balloon_text))

    def set_animation(self, animation_name, frames, fps=10.0, with_audio=False, synced=False, timeline_entries=None, balloon_text=None):
        self.play(animation_name, frames, fps=fps, with_audio=with_audio, synced=synced, timeline_entries=timeline_entries, balloon_text=balloon_text)

    def say(self, text):
        self._start()
        self._commands.put(("say", text))

    def clear_balloon(self):
        if self._thread is None or not self._thread.is_alive():
            return
        self.say(None)

    def stop(self):
        if self._thread is not None and self._thread.is_alive():
            self._commands.put(("stop",))

    def set_sync_debug(self, enabled):
        self._default_sync_debug = bool(enabled)
        with self._status_lock:
            self._status["debug_sync"] = self._default_sync_debug
            self._status["player_created"] = self._thread is not None and self._thread.is_alive()
        if self._thread is not None and self._thread.is_alive():
            self._commands.put(("debug-sync", bool(enabled)))

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
            f"Overlay: {'on' if status['overlay'] else 'off'}",
            f"Position: {status['x'] if status['x'] is not None else '-'}, {status['y'] if status['y'] is not None else '-'}",
            f"Window size: {status['window_width'] if status['window_width'] is not None else '-'} x {status['window_height'] if status['window_height'] is not None else '-'}",
            f"Current frame: {status['frame_index'] if status['frame_index'] is not None else '-'}",
            f"Frames: {status['frame_count']}",
            f"Balloon: {status['balloon_text'] if status['balloon_text'] is not None else '-'}",
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

def interactive_shell(character_path: str | None = None, overlay: bool = False, x: int | None = None, y: int | None = None) -> None:
    runtime = get_runtime(character_path)
    player = AnimationPlayer(runtime)
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
                if command == "say":
                    text = raw_line.strip()[3:].strip()
                    if not text:
                        print("error: say requires text")
                        continue
                    player.say(text)
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
        "  say <text>               -> show or replace dialogue text",
        "  say-clear                -> hide the dialogue bubble",
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
    "say-clear": [
        "say-clear",
        "Hide the current dialogue bubble without closing the window.",
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
    args = parser.parse_args(argv)
    global BALLOON_DEBUG
    BALLOON_DEBUG = bool(args.balloon_debug)
    overlay_mode = bool(args.overlay or args.x is not None or args.y is not None)

    if args.interactive:
        try:
            interactive_shell(args.acs_file, overlay=overlay_mode, x=args.x, y=args.y)
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
