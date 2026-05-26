import argparse
import bisect
import csv
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import mido


@dataclass
class LyricEvent:
    time_seconds: float
    text: str
    source_line: int


@dataclass
class NoteEvent:
    track: int
    index: int
    abs_tick: int
    time_seconds: float
    note: int
    velocity: int
    channel: int
    end_tick: Optional[int] = None
    end_time_seconds: Optional[float] = None


@dataclass
class AlignmentResult:
    lyric: LyricEvent
    matched_note: Optional[NoteEvent]
    delta_seconds: Optional[float]
    accepted: bool


class TempoMap:
    """Tempo-aware conversion between MIDI ticks and seconds."""

    def __init__(self, ticks_per_beat: int, tempo_events: List[Tuple[int, int]]):
        self.ticks_per_beat = ticks_per_beat
        self.tempo_events = self._normalize_tempo_events(tempo_events)
        self._segments = self._build_segments()

    @staticmethod
    def _normalize_tempo_events(tempo_events: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
        merged = {}
        for tick, tempo in tempo_events:
            merged[int(tick)] = int(tempo)

        if 0 not in merged:
            merged[0] = 500000

        return sorted(merged.items(), key=lambda x: x[0])

    def _build_segments(self):
        segments = []
        accumulated_seconds = 0.0

        for i, (tick, tempo) in enumerate(self.tempo_events):
            next_tick = self.tempo_events[i + 1][0] if i + 1 < len(self.tempo_events) else None
            segments.append((tick, tempo, accumulated_seconds, next_tick))

            if next_tick is not None and next_tick > tick:
                delta_ticks = next_tick - tick
                accumulated_seconds += (delta_ticks * tempo) / (self.ticks_per_beat * 1_000_000.0)

        return segments

    @classmethod
    def from_midi(cls, midi_file: mido.MidiFile) -> "TempoMap":
        tempo_events = []
        for track in midi_file.tracks:
            abs_tick = 0
            for msg in track:
                abs_tick += msg.time
                if msg.type == "set_tempo":
                    tempo_events.append((abs_tick, msg.tempo))

        return cls(midi_file.ticks_per_beat, tempo_events)

    def tick_to_seconds(self, tick: int) -> float:
        tick = int(tick)
        current = self._segments[0]
        for seg in self._segments:
            start_tick, _tempo, _start_sec, end_tick = seg
            if end_tick is None or tick < end_tick:
                current = seg
                break
            current = seg

        start_tick, tempo, start_seconds, _end_tick = current
        delta_ticks = max(0, tick - start_tick)
        return start_seconds + (delta_ticks * tempo) / (self.ticks_per_beat * 1_000_000.0)


_TIMESTAMP_RE = re.compile(r"^\s*(\d+):(\d+(?:[.,]\d+)?)\s*$")
_HMS_TIMESTAMP_RE = re.compile(r"^\s*(\d+):(\d+):(\d+(?:[.,]\d+)?)\s*$")
_LRC_TIMESTAMP_RE = re.compile(r"\[(\d+):(\d+(?:[.,]\d+)?)\]")
_KARAOKE_ADD_RE = re.compile(
    r"""karaoke\.add\(\s*(['"])(.*?)\1\s*,\s*(['"])(.*?)\3\s*,\s*(['"])(.*?)\5\s*,\s*(['"])(.*?)\7\s*\)\s*;?""",
    re.IGNORECASE,
)
_SUSTAIN_MARKERS = {"-", "－", "—", "–", "_", "~", "～", "…", "・・・", "ー", "ㅡ", "."}


def parse_time_to_seconds(value: str) -> float:
    raw_value = value.strip()
    if not raw_value:
        raise ValueError("empty timestamp")

    value = raw_value.replace(",", ".")

    # pure seconds
    try:
        seconds = float(value)
        if ":" not in value and re.fullmatch(r"\d+(?:\.\d+)?", value):
            if seconds >= 10000 and float(int(seconds)) == seconds:
                return seconds / 1000.0
        return seconds
    except ValueError:
        pass

    m_hms = _HMS_TIMESTAMP_RE.match(value)
    if m_hms:
        hours = int(m_hms.group(1))
        minutes = int(m_hms.group(2))
        seconds = float(m_hms.group(3).replace(",", "."))
        return hours * 3600 + minutes * 60 + seconds

    m_ms = _TIMESTAMP_RE.match(value)
    if m_ms:
        minutes = int(m_ms.group(1))
        seconds = float(m_ms.group(2).replace(",", "."))
        return minutes * 60 + seconds

    raise ValueError(f"invalid timestamp format: {raw_value}")


def _expand_events_to_units(events: List[LyricEvent]) -> List[LyricEvent]:
    expanded: List[LyricEvent] = []

    for i, e in enumerate(events):
        text = normalize_lyric_text(e.text)
        if not text:
            continue

        units = _split_lyric_units(text)
        if len(units) <= 1:
            expanded.append(LyricEvent(time_seconds=e.time_seconds, text=text, source_line=e.source_line))
            continue

        start = float(e.time_seconds)
        end = None
        if i + 1 < len(events) and events[i + 1].time_seconds > start:
            end = float(events[i + 1].time_seconds)
        elif i - 1 >= 0 and start > events[i - 1].time_seconds:
            end = start + (start - float(events[i - 1].time_seconds))
        else:
            end = start + max(0.1, 0.05 * len(units))

        span = max(0.0, end - start)
        step = span / float(len(units)) if units else 0.0

        for j, u in enumerate(units):
            u2 = normalize_lyric_text(u)
            if not u2:
                continue
            expanded.append(LyricEvent(time_seconds=start + j * step, text=u2, source_line=e.source_line))

    expanded.sort(key=lambda x: x.time_seconds)
    return expanded


def parse_lyric_file(lyric_file_path: str, split_units: bool = False) -> List[LyricEvent]:
    ext = os.path.splitext(lyric_file_path)[1].lower()
    is_karaoke = False
    if ext == ".lrc":
        events = _parse_lrc_file(lyric_file_path)
    else:
        with open(lyric_file_path, "r", encoding="utf-8-sig") as f:
            sample = f.read(4096)
        if "karaoke.add(" in sample.lower():
            is_karaoke = True
            events = _parse_karaoke_script_file(lyric_file_path)
        else:
            events = _parse_delimited_file(lyric_file_path)

    events.sort(key=lambda x: x.time_seconds)
    if split_units and not is_karaoke:
        events = _expand_events_to_units(events)
    return events


def _parse_lrc_file(path: str) -> List[LyricEvent]:
    events: List[LyricEvent] = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue

            timestamps = list(_LRC_TIMESTAMP_RE.finditer(line))
            if not timestamps:
                continue

            text = _LRC_TIMESTAMP_RE.sub("", line).strip()
            if not text:
                continue

            for match in timestamps:
                ts = f"{match.group(1)}:{match.group(2)}"
                try:
                    seconds = parse_time_to_seconds(ts)
                except ValueError:
                    continue
                events.append(LyricEvent(time_seconds=seconds, text=text, source_line=line_no))

    return events


def _parse_delimited_file(path: str) -> List[LyricEvent]:
    events: List[LyricEvent] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)

        delimiter = None
        if "\t" in sample:
            delimiter = "\t"
        elif "," in sample:
            delimiter = ","

        if delimiter:
            reader = csv.reader(f, delimiter=delimiter)
            for line_no, row in enumerate(reader, start=1):
                if not row or len(row) < 2:
                    continue
                time_raw = row[0].strip()
                text = row[1].strip()
                if not text:
                    continue
                try:
                    seconds = parse_time_to_seconds(time_raw)
                except ValueError:
                    # skip likely header line
                    if line_no == 1:
                        continue
                    continue
                events.append(LyricEvent(time_seconds=seconds, text=text, source_line=line_no))
        else:
            # fallback: split by first whitespace
            for line_no, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line:
                    continue
                parts = line.split(maxsplit=1)
                if len(parts) < 2:
                    continue
                time_raw, text = parts[0].strip(), parts[1].strip()
                if not text:
                    continue
                try:
                    seconds = parse_time_to_seconds(time_raw)
                except ValueError:
                    if line_no == 1:
                        continue
                    continue
                events.append(LyricEvent(time_seconds=seconds, text=text, source_line=line_no))

    return events


def _split_lyric_units(text: str) -> List[str]:
    units: List[str] = []
    last_end = 0

    def process_outside(s: str):
        # Match English words (including apostrophes) OR any single non-whitespace character
        for m in re.finditer(r"([a-zA-Z0-9_']+)|([^\s])", s):
            if m.group(1):
                units.append(m.group(1))
            elif m.group(2):
                units.append(m.group(2))

    for m in re.finditer(r"\[([^\]]*)\]", text):
        outside = text[last_end : m.start()]
        process_outside(outside)

        inside = m.group(1).strip()
        if inside:
            units.append(inside)
        last_end = m.end()

    tail = text[last_end:]
    process_outside(tail)

    if units:
        return units

    stripped = text.strip()
    return [stripped] if stripped else []


def normalize_lyric_text(text: str) -> str:
    s = text.strip()
    if not s:
        return ""
    if s in _SUSTAIN_MARKERS:
        return "-"
    return s


def _parse_karaoke_script_file(path: str) -> List[LyricEvent]:
    events: List[LyricEvent] = []

    with open(path, "r", encoding="utf-8-sig") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line or "karaoke.add(" not in line.lower():
                continue

            m = _KARAOKE_ADD_RE.search(line)
            if not m:
                continue

            start_raw = m.group(2).strip()
            end_raw = m.group(4).strip()
            text_raw = m.group(6)
            duration_raw = m.group(8).strip()

            try:
                start_seconds = parse_time_to_seconds(start_raw)
                end_seconds = parse_time_to_seconds(end_raw)
            except ValueError:
                continue

            units = _split_lyric_units(text_raw)
            if not units:
                continue

            durations_ms = []
            if duration_raw:
                for token in duration_raw.split(","):
                    token = token.strip()
                    if not token:
                        continue
                    try:
                        durations_ms.append(int(token))
                    except ValueError:
                        pass

            # Prefer explicit per-unit durations when count matches.
            if len(durations_ms) == len(units):
                current = start_seconds
                for unit, d_ms in zip(units, durations_ms):
                    events.append(LyricEvent(time_seconds=current, text=unit, source_line=line_no))
                    current += max(0, d_ms) / 1000.0
                continue

            # Fallback: evenly place units in [start, end] interval.
            total = max(0.0, end_seconds - start_seconds)
            step = (total / len(units)) if units else 0.0
            current = start_seconds
            for unit in units:
                events.append(LyricEvent(time_seconds=current, text=unit, source_line=line_no))
                current += step

    return events


def choose_target_track(midi_file: mido.MidiFile) -> int:
    best_idx = 0
    best_count = -1

    for i, track in enumerate(midi_file.tracks):
        count = 0
        for msg in track:
            if msg.type == "note_on" and getattr(msg, "velocity", 0) > 0:
                count += 1
        if count > best_count:
            best_count = count
            best_idx = i

    return best_idx


def extract_note_events(midi_file: mido.MidiFile, tempo_map: TempoMap, track_index: int) -> List[NoteEvent]:
    notes: List[NoteEvent] = []

    if track_index < 0 or track_index >= len(midi_file.tracks):
        raise IndexError(f"track index out of range: {track_index}")

    abs_tick = 0
    note_idx = 0
    active_notes = {}
    
    for msg in midi_file.tracks[track_index]:
        abs_tick += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            ne = NoteEvent(
                track=track_index,
                index=note_idx,
                abs_tick=abs_tick,
                time_seconds=tempo_map.tick_to_seconds(abs_tick),
                note=msg.note,
                velocity=msg.velocity,
                channel=msg.channel,
            )
            notes.append(ne)
            active_notes[(msg.note, msg.channel)] = ne
            note_idx += 1
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            key = (msg.note, msg.channel)
            if key in active_notes:
                ne = active_notes[key]
                ne.end_tick = abs_tick
                ne.end_time_seconds = tempo_map.tick_to_seconds(abs_tick)
                del active_notes[key]

    for ne in notes:
        if ne.end_tick is None:
            ne.end_tick = ne.abs_tick
            ne.end_time_seconds = ne.time_seconds

    return notes


def _estimate_global_offset(lyric_times: List[float], note_times: List[float], max_allowed: float = 10.0) -> float:
    """估计歌词与音符之间的整体时间偏移量（采用直方图统计算法，支持较大偏移）"""
    if not lyric_times or not note_times:
        return 0.0
    
    diffs = []
    for lt in lyric_times:
        # 只统计容差范围内的可能偏移量
        idx_start = bisect.bisect_left(note_times, lt - max_allowed)
        idx_end = bisect.bisect_right(note_times, lt + max_allowed)
        for j in range(idx_start, idx_end):
            diffs.append(note_times[j] - lt)
            
    if not diffs:
        return 0.0
        
    # 将偏移量划分到 50ms 的区间（bin）中进行投票
    # 引入高斯权重惩罚：距离 0 越远的偏移量，其投票权重越低
    # 这能防止在节奏重复的歌曲中，将错误的一个小节长度（如 +2.0s）误判为全局偏移
    bins = {}
    bin_size = 0.05
    import math
    for d in diffs:
        b = round(d / bin_size)
        # 高斯衰减方差设为 50.0
        # d=0.0 时 weight=1.0; d=5.0 时 weight=0.6; d=10.0 时 weight=0.13
        weight = math.exp(-(d**2) / 50.0)
        bins[b] = bins.get(b, 0.0) + weight
        
    # 找出加权票数最多的偏移量区间
    best_bin = max(bins, key=bins.get)
    best_diffs = [d for d in diffs if round(d / bin_size) == best_bin]
    
    # 在最优区间内取中位数，以获得精确且抗噪的偏移值
    best_diffs.sort()
    return best_diffs[len(best_diffs) // 2]


def _estimate_local_offsets(lyric_times: List[float], note_times: List[float], global_offset: float) -> List[float]:
    """估计局部的动态时间偏移量，以应对节拍波动和渐变的时间差"""
    if not lyric_times or not note_times:
        return [0.0] * len(lyric_times)
        
    shifted_lyric_times = [t + global_offset for t in lyric_times]
    
    anchors = []
    for i, lt in enumerate(shifted_lyric_times):
        idx = bisect.bisect_left(note_times, lt)
        cands = []
        if idx < len(note_times):
            cands.append((idx, note_times[idx]))
        if idx > 0:
            cands.append((idx - 1, note_times[idx - 1]))
            
        if not cands:
            continue
            
        best_j, best_nt = min(cands, key=lambda x: abs(x[1] - lt))
        
        # 检查是否互为最近邻
        l_idx = bisect.bisect_left(shifted_lyric_times, best_nt)
        l_cands = []
        if l_idx < len(shifted_lyric_times):
            l_cands.append((l_idx, shifted_lyric_times[l_idx]))
        if l_idx > 0:
            l_cands.append((l_idx - 1, shifted_lyric_times[l_idx - 1]))
            
        if not l_cands:
            continue
            
        best_i, best_lt = min(l_cands, key=lambda x: abs(x[1] - best_nt))
        
        # 如果互为最近邻且时间差小于阈值（例如 200ms），则作为可靠锚点
        if best_i == i and abs(best_nt - lt) < 0.2:
            anchors.append((i, best_nt - lt))
            
    if not anchors:
        return [0.0] * len(lyric_times)
        
    # 线性插值
    anchor_indices = [a[0] for a in anchors]
    anchor_offsets = [a[1] for a in anchors]
    
    local_offsets = []
    for i in range(len(lyric_times)):
        if i <= anchor_indices[0]:
            local_offsets.append(anchor_offsets[0])
        elif i >= anchor_indices[-1]:
            local_offsets.append(anchor_offsets[-1])
        else:
            idx = bisect.bisect_left(anchor_indices, i)
            i0, i1 = anchor_indices[idx-1], anchor_indices[idx]
            o0, o1 = anchor_offsets[idx-1], anchor_offsets[idx]
            if i1 == i0:
                local_offsets.append(o0)
            else:
                oi = o0 + (o1 - o0) * (i - i0) / (i1 - i0)
            local_offsets.append(oi)
            
    return local_offsets


def align_lyrics_to_notes(
    lyric_events: List[LyricEvent],
    note_events: List[NoteEvent],
    tolerance_ms: int = 220,
) -> List[AlignmentResult]:
    tolerance_sec = tolerance_ms / 1000.0
    M = len(lyric_events)
    N = len(note_events)

    if M == 0 or N == 0:
        return [AlignmentResult(lyric=e, matched_note=None, delta_seconds=None, accepted=False) for e in lyric_events]

    note_times = [n.time_seconds for n in note_events]
    original_lyric_times = [l.time_seconds for l in lyric_events]

    # 1. 全局偏移校正：计算并应用中位数偏移，消除整体时间差
    global_offset = _estimate_global_offset(original_lyric_times, note_times, max_allowed=10.0)
    # 如果偏移较小（例如小于 10ms），则忽略
    if abs(global_offset) < 0.01:
        global_offset = 0.0
        
    # 2. 局部偏移校正：通过锚点插值补偿局部的节拍波动
    local_offsets = _estimate_local_offsets(original_lyric_times, note_times, global_offset)
    
    lyric_times = [original_lyric_times[i] + global_offset + local_offsets[i] for i in range(M)]

    # 动态规划表：dp[i][j] 记录前 i 个歌词和前 j 个音符的最优匹配 (最大匹配数, 最小总时间误差即最大负误差)
    dp = [[(0, 0.0)] * (N + 1) for _ in range(M + 1)]
    # 状态回溯表：1 = match, 2 = skip lyric, 3 = skip note
    tb = [[0] * (N + 1) for _ in range(M + 1)]

    for i in range(1, M + 1):
        l_time = lyric_times[i - 1]
        
        # 优化：寻找当前歌词在容差范围内的有效音符索引区间，缩小判断范围
        min_j = bisect.bisect_left(note_times, l_time - tolerance_sec)
        max_j = bisect.bisect_right(note_times, l_time + tolerance_sec)
        
        for j in range(1, N + 1):
            # 选项 1：跳过当前音符 j，继承左侧状态
            best_m, best_e = dp[i][j - 1]
            action = 3
            
            # 选项 2：跳过当前歌词 i，继承上方状态
            sm, se = dp[i - 1][j]
            if sm > best_m or (sm == best_m and se > best_e):
                best_m, best_e = sm, se
                action = 2
                
            # 选项 3：匹配当前歌词 i 和音符 j
            if min_j <= j - 1 < max_j:
                diff = abs(l_time - note_times[j - 1])
                if diff <= tolerance_sec:
                    pm, pe = dp[i - 1][j - 1]
                    match_m = pm + 1
                    match_e = pe - diff  # 误差取负数，使得最大化等于误差最小化
                    if match_m > best_m or (match_m == best_m and match_e > best_e):
                        best_m, best_e = match_m, match_e
                        action = 1
                        
            dp[i][j] = (best_m, best_e)
            tb[i][j] = action

    # 从右下角回溯找出最优匹配路径
    matches = {}
    i, j = M, N
    while i > 0 and j > 0:
        action = tb[i][j]
        if action == 1:
            matches[i - 1] = j - 1
            i -= 1
            j -= 1
        elif action == 2:
            i -= 1
        elif action == 3:
            j -= 1
        else:
            break

    results = []
    for idx in range(M):
        if idx in matches:
            n_idx = matches[idx]
            # 计算 delta 时使用最原始的歌词时间，反映真实的物理偏差
            delta = note_times[n_idx] - original_lyric_times[idx]
            results.append(
                AlignmentResult(
                    lyric=lyric_events[idx],
                    matched_note=note_events[n_idx],
                    delta_seconds=delta,
                    accepted=True,
                )
            )
        else:
            results.append(
                AlignmentResult(
                    lyric=lyric_events[idx],
                    matched_note=None,
                    delta_seconds=None,
                    accepted=False,
                )
            )

    return results


def write_lyrics_to_track(
    midi_file: mido.MidiFile,
    track_index: int,
    note_events: List[NoteEvent],
    alignments: List[AlignmentResult],
    clear_existing_lyrics: bool = True,
    fill_sustain_dash: bool = True,
):
    track = midi_file.tracks[track_index]

    abs_items = []
    abs_tick = 0
    for msg in track:
        abs_tick += msg.time
        if clear_existing_lyrics and msg.type in ("lyrics", "text"):
            continue
        abs_items.append((abs_tick, msg))

    inserted_note_indices = set()
    sustain_inserted = 0

    for order, item in enumerate(alignments):
        if not item.accepted or item.matched_note is None:
            continue
        text = normalize_lyric_text(item.lyric.text)
        if not text:
            continue
        lyric_msg = mido.MetaMessage("lyrics", text=text, time=0)
        abs_items.append((item.matched_note.abs_tick, lyric_msg, order))
        inserted_note_indices.add(item.matched_note.index)

    if fill_sustain_dash:
        matched_indices = sorted(
            {
                item.matched_note.index
                for item in alignments
                if item.accepted and item.matched_note is not None
            }
        )
        dash_order = len(alignments) + 1
        for left, right in zip(matched_indices, matched_indices[1:]):
            if right <= left + 1:
                continue
            for note_idx in range(left + 1, right):
                if note_idx in inserted_note_indices:
                    continue
                if note_idx < 0 or note_idx >= len(note_events):
                    continue
                
                note = note_events[note_idx]
                prev_note = note_events[note_idx - 1]
                
                # 如果当前音符与上一个音符的结束时间之间存在明显停顿（例如大于 0.25 秒），
                # 则认为是乐句断句（休止符/呼吸），此时应打断延音符号的连续填充，
                # 避免 ACE Studio 跨越超长休止符将上一个音符强行拉长。
                rest_duration = note.time_seconds - prev_note.end_time_seconds
                if rest_duration > 0.25:
                    break
                    
                dash_msg = mido.MetaMessage("lyrics", text="-", time=0)
                abs_items.append((note.abs_tick, dash_msg, dash_order))
                dash_order += 1
                sustain_inserted += 1

    # normalize tuples to a common shape: (tick, priority, order, message)
    normalized = []
    existing_counter = 0
    for row in abs_items:
        if len(row) == 2:
            tick, msg = row
            priority = 2 if msg.type == "end_of_track" else 1
            normalized.append((tick, priority, 1000000 + existing_counter, msg))
            existing_counter += 1
        else:
            tick, msg, order = row
            normalized.append((tick, 0, order, msg))

    normalized.sort(key=lambda x: (x[0], x[1], x[2]))

    new_track = mido.MidiTrack()
    last_tick = 0
    for tick, _priority, _order, msg in normalized:
        delta = int(tick - last_tick)
        copied = msg.copy(time=delta)
        new_track.append(copied)
        last_tick = tick

    midi_file.tracks[track_index] = new_track
    return sustain_inserted


def align_lyrics_to_midi(
    midi_file_path: str,
    lyric_file_path: str,
    output_midi_path: Optional[str] = None,
    tolerance_ms: int = 220,
    target_track: Optional[int] = None,
    clear_existing_lyrics: bool = True,
    fill_sustain_dash: bool = True,
    split_units: bool = False,
):
    midi_file = mido.MidiFile(midi_file_path)
    tempo_map = TempoMap.from_midi(midi_file)

    lyric_events = parse_lyric_file(lyric_file_path, split_units=split_units)

    if target_track is None:
        target_track = choose_target_track(midi_file)

    note_events = extract_note_events(midi_file, tempo_map, target_track)
    alignments = align_lyrics_to_notes(lyric_events, note_events, tolerance_ms=tolerance_ms)

    sustain_inserted = write_lyrics_to_track(
        midi_file,
        track_index=target_track,
        note_events=note_events,
        alignments=alignments,
        clear_existing_lyrics=clear_existing_lyrics,
        fill_sustain_dash=fill_sustain_dash,
    )

    if output_midi_path is None:
        base, ext = os.path.splitext(midi_file_path)
        output_midi_path = f"{base}_with_lyrics{ext or '.mid'}"

    # Preserve multilingual lyrics (Chinese/Korean/English) when writing meta text.
    midi_file.charset = "utf-8"
    midi_file.save(output_midi_path)

    accepted = [a for a in alignments if a.accepted]
    rejected = [a for a in alignments if not a.accepted]

    diagnosis = ""
    if not lyric_events:
        diagnosis = "未从歌词文件中解析到任何带时间戳的歌词事件（请检查格式/分隔符/时间戳写法）。"
    elif not note_events:
        diagnosis = f"目标轨道 {target_track} 未检测到可用的 note_on 音符（可能选到了只有节拍/控制/元信息的轨道）。"
    elif len(accepted) == 0:
        lyric_last = lyric_events[-1].time_seconds
        note_last = note_events[-1].time_seconds
        if note_last > 0 and lyric_last > max(1000.0, note_last * 50.0):
            diagnosis = "歌词时间范围远大于MIDI音符时间范围，疑似歌词时间单位为毫秒但被按秒解析（例如 13196 实际应为 13.196s）。"
        else:
            approx_offset = note_events[0].time_seconds - lyric_events[0].time_seconds
            diagnosis = f"当前容差 {tolerance_ms}ms 下未匹配到音符，首个时间点大致偏移 {approx_offset:.3f}s（请检查是否选错轨道/歌词与MIDI是否同版本）。"

    report = {
        "output_midi_path": output_midi_path,
        "target_track": target_track,
        "total_lyrics": len(lyric_events),
        "total_notes": len(note_events),
        "matched_lyrics": len(accepted),
        "unmatched_lyrics": len(rejected),
        "sustain_dash_inserted": sustain_inserted,
        "tolerance_ms": tolerance_ms,
        "diagnosis": diagnosis,
        "alignments": alignments,
    }
    return report


def _main():
    parser = argparse.ArgumentParser(description="将歌词时间文件自动吸附到MIDI音符并写入lyrics事件")
    parser.add_argument("midi_file", help="输入MIDI文件路径")
    parser.add_argument("lyric_file", help="歌词时间文件路径（支持 lrc / csv / txt）")
    parser.add_argument("--output", "-o", help="输出MIDI路径")
    parser.add_argument("--tolerance-ms", type=int, default=220, help="吸附容差（毫秒），默认220")
    parser.add_argument("--track", default="auto", help="目标轨道，默认auto（音符最多轨道）")
    parser.add_argument("--keep-existing", action="store_true", help="保留原有lyrics/text，不清除")
    parser.add_argument("--no-sustain-dash", action="store_true", help="不自动填充延音符号 '-'")
    parser.add_argument("--split-units", action="store_true", help="将每行歌词拆分为字/词并分别吸附（karaoke脚本默认已拆分）")
    args = parser.parse_args()

    target_track = None if str(args.track).lower() == "auto" else int(args.track)
    report = align_lyrics_to_midi(
        midi_file_path=args.midi_file,
        lyric_file_path=args.lyric_file,
        output_midi_path=args.output,
        tolerance_ms=args.tolerance_ms,
        target_track=target_track,
        clear_existing_lyrics=not args.keep_existing,
        fill_sustain_dash=not args.no_sustain_dash,
        split_units=bool(args.split_units),
    )

    print(f"输出文件: {report['output_midi_path']}")
    print(f"目标轨道: {report['target_track']}")
    print(f"歌词总数: {report['total_lyrics']}")
    print(f"音符总数: {report['total_notes']}")
    print(f"成功匹配: {report['matched_lyrics']}")
    print(f"未匹配: {report['unmatched_lyrics']}")
    print(f"自动填充延音'-': {report['sustain_dash_inserted']}")
    print(f"吸附容差: {report['tolerance_ms']}ms")


if __name__ == "__main__":
    _main()
