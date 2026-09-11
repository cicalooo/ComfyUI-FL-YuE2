"""Inspect the limited two-voice ABC dialect used by YuE2 and SheetSage2.

Original, standard-library-only implementation. This is deliberately not a
general ABC parser or a replacement for SheetSage2's native score serializer.
All times are exact fractions of a quarter note. See references/abc-editing.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction


VOICES = ("Vocal", "Ins")


HEADER_VOCAL = 'V: Vocal clef=treble name="Vocal Melody" snm="Vocal"'
HEADER_INS = 'V: Ins clef=treble name="Ins Melody" snm="Inst."'
_INS_ALIASES = {
    "ins", "inst", "inst.", "instrumental", "instrument", "instruments",
    "melody", "lead", "accompaniment", "acc",
}


def _normalize_voice_line(line: str) -> str:
    """Map common LLM voice spellings onto the two native V: tags / headers."""
    match = re.match(r"^V:\s*(.+?)\s*$", line)
    if match is None:
        return line
    body = match.group(1).strip()
    lower = body.lower()
    if lower.startswith("vocal clef=") or lower == HEADER_VOCAL.lower():
        return HEADER_VOCAL
    if "clef=" in lower and (
        lower.startswith("ins ")
        or lower.startswith("inst")
        or "ins melody" in lower
        or "inst." in lower
        or lower.startswith("instrument")
    ):
        return HEADER_INS
    if lower == "vocal":
        return "V: Vocal"
    # Bare voice switch aliases for the instrumental voice.
    token = lower.split()[0].rstrip(".")
    if lower in _INS_ALIASES or token in _INS_ALIASES:
        return "V: Ins"
    return f"V: {body}"


def normalize_native_abc(text: str) -> str:
    """Repair frequent LLM layout mistakes before the strict native parse.

    - Normalize newlines / trim blank lines
    - Fix V:Ins / V: Instrumental style aliases
    - Insert a missing ``V: Ins`` when a music line follows Vocal music directly
    - Drop section comments that were placed between Vocal and Ins in a group
    - Fit each measure to M:/L: (halve/double common LLM grids; trim/pad overflows)
    """
    if not isinstance(text, str):
        return text
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "").strip()
    # Prefer a fenced ```abc block if the model wrapped the score.
    fenced = re.search(r"```(?:abc)?\s*\n([\s\S]*?)```", text, re.IGNORECASE)
    if fenced and re.search(r"(?m)^X:\s*1\s*$", fenced.group(1)):
        text = fenced.group(1).strip()
    labeled = re.search(
        r"###\s*SCORE_ABC\s*\n([\s\S]*?)(?=\n###\s+[A-Z]|$)",
        text,
        re.IGNORECASE,
    )
    if labeled and re.search(r"(?m)^X:\s*1\s*$", labeled.group(1)):
        text = labeled.group(1).strip()
    raw_lines = [_normalize_voice_line(line.strip()) for line in text.splitlines()]
    lines = [line for line in raw_lines if line]
    if len(lines) < 8:
        return "\n".join(lines) + ("\n" if lines else "")

    header, body = lines[:8], lines[8:]
    # Soft-fix exact header voice definitions when close.
    if len(header) >= 7:
        if header[5].startswith("V:") and header[5] != HEADER_VOCAL:
            if "vocal" in header[5].lower():
                header[5] = HEADER_VOCAL
        if header[6].startswith("V:") and header[6] != HEADER_INS:
            if any(token in header[6].lower() for token in ("ins", "inst", "instrument")):
                header[6] = HEADER_INS

    repaired: list[str] = []
    i = 0
    while i < len(body):
        line = body[i]
        if line.startswith("%"):
            # Force "% name" form.
            if line.startswith("% "):
                repaired.append(line)
            else:
                repaired.append("% " + line[1:].lstrip())
            i += 1
            continue
        if line == "V: Vocal":
            repaired.append(line)
            i += 1
            while i < len(body) and body[i].startswith(("M:", "K:")):
                repaired.append(body[i])
                i += 1
            if i < len(body) and body[i].endswith("|") and not body[i].startswith("V:"):
                repaired.append(body[i])
                i += 1
            # Section comments illegally sitting between Vocal and Ins → skip for now.
            while i < len(body) and body[i].startswith("%"):
                i += 1
            if i < len(body) and body[i] == "V: Ins":
                repaired.append(body[i])
                i += 1
            elif i < len(body) and body[i].endswith("|") and not body[i].startswith("V:"):
                repaired.append("V: Ins")
            elif i < len(body) and body[i].startswith("V:"):
                # Unknown V: tag after Vocal music — coerce instrumental aliases only.
                repaired.append("V: Ins")
                i += 1
            else:
                # Let the strict parser report a clear missing-music error.
                repaired.append("V: Ins")
            while i < len(body) and body[i].startswith(("M:", "K:")):
                repaired.append(body[i])
                i += 1
            if i < len(body) and body[i].endswith("|") and not body[i].startswith(("V:", "%")):
                repaired.append(body[i])
                i += 1
            continue
        repaired.append(line)
        i += 1

    repaired = _repair_score_durations(header, repaired)
    return "\n".join(header + repaired) + "\n"

DURATIONS = {1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48}
QUALITIES = ("", "m", "dim", "aug", "7", "maj7", "m7", "dim7", "m7b5",
             "sus4", "sus2", "6", "m6", "7sus4", "m(maj7)")
PITCH_NAME = r"[A-G](?:bb|##|b|#)?"
CHORD = re.compile(PITCH_NAME + "(?:" + "|".join(re.escape(q) for q in QUALITIES)
                   + ")(?:/" + PITCH_NAME + ")?")
TOKEN = re.compile(
    r'"(?P<chord>[^"\n]*)"|\[K:(?P<key>[^\]\n]+)\]|'
    r"(?P<acc>\^\^|__|\^|_|=)?(?P<note>[A-Ga-gz])"
    r"(?P<oct>[,']*)(?P<duration>[0-9]*)(?P<tie>-?)"
)


def _units_per_bar(meter_field: str, length_field: str) -> int | None:
    """L-unit count that must fill one measure (e.g. M:4/4 + L:1/16 -> 16)."""
    meter = re.fullmatch(r"M:([1-9][0-9]*)/([1-9][0-9]*)", meter_field)
    length = re.fullmatch(r"L:1/([1-9][0-9]*)", length_field)
    if meter is None or length is None:
        return None
    n, d = int(meter.group(1)), int(meter.group(2))
    denom = int(length.group(1))
    if d == 0 or (n * denom) % d != 0:
        return None
    return (n * denom) // d


def _rewrite_duration(token: str, new_dur: int) -> str:
    match = TOKEN.match(token)
    if match is None or match.group("note") is None:
        return token
    prefix = token[: match.start("duration")]
    # TOKEN duration group may be empty; end before tie.
    tie = match.group("tie") or ""
    body = match.group("note")
    # Rebuild from structured groups to avoid slicing hazards.
    acc = match.group("acc") or ""
    octv = match.group("oct") or ""
    dur = "" if new_dur == 1 else str(new_dur)
    return f"{acc}{body}{octv}{dur}{tie}"


def _split_rests(units: int) -> list[str] | None:
    if units < 0:
        return None
    if units == 0:
        return []
    parts: list[str] = []
    remaining = units
    for length in sorted(DURATIONS, reverse=True):
        while remaining >= length:
            remaining -= length
            parts.append("z" if length == 1 else f"z{length}")
    return None if remaining else parts


def _bar_note_items(bar: str):
    """Return list of (kind, token, duration_units) or None if untokenizable."""
    if bar == "Z" or re.fullmatch(r"Z[2-4]", bar):
        return [("full", bar, None)]
    cursor = 0
    items = []
    while cursor < len(bar):
        if bar[cursor].isspace():
            cursor += 1
            continue
        match = TOKEN.match(bar, cursor)
        if match is None:
            return None
        token = match.group(0)
        cursor = match.end()
        if match.group("chord") is not None or match.group("key") is not None:
            items.append(("meta", token, 0))
            continue
        dur = int(match.group("duration") or "1")
        items.append(("note", token, dur))
    return items


def _repair_bar(bar: str, target: int) -> str:
    bar = bar.strip()
    if not bar:
        return bar
    items = _bar_note_items(bar)
    if items is None:
        return bar
    if items and items[0][0] == "full":
        return bar
    total = sum(dur for kind, _, dur in items if kind == "note")
    if total == target:
        return "".join(token for _, token, _ in items)

    # Common LLM mistake: durations written at 2x or 1/2x the L: grid.
    if total == 2 * target and all(dur % 2 == 0 for kind, _, dur in items if kind == "note"):
        return "".join(
            _rewrite_duration(token, dur // 2) if kind == "note" else token
            for kind, token, dur in items
        )
    if total * 2 == target and all((dur * 2) in DURATIONS for kind, _, dur in items if kind == "note"):
        return "".join(
            _rewrite_duration(token, dur * 2) if kind == "note" else token
            for kind, token, dur in items
        )

    if total > target:
        out: list[str] = []
        used = 0
        for kind, token, dur in items:
            if kind != "note":
                out.append(token)
                continue
            if used >= target:
                break
            if used + dur <= target:
                out.append(token)
                used += dur
                continue
            need = target - used
            if need in DURATIONS:
                out.append(_rewrite_duration(token, need))
                used = target
            break
        if used < target:
            rests = _split_rests(target - used)
            if rests is None:
                return bar
            out.extend(rests)
        return "".join(out)

    rests = _split_rests(target - total)
    if rests is None:
        return bar
    return "".join(token for _, token, _ in items) + "".join(rests)


def _repair_music_line(line: str, target: int) -> str:
    if target is None or not line.endswith("|"):
        return line
    bars = line[:-1].split("|")
    fixed = []
    for bar in bars:
        if bar.strip() == "" and not fixed:
            continue
        fixed.append(_repair_bar(bar, target))
    # Preserve trailing barline; drop accidental empty leading pieces.
    return "|".join(fixed) + "|"


def _repair_score_durations(header: list[str], body: list[str]) -> list[str]:
    if len(header) < 4:
        return body
    target = _units_per_bar(header[2], header[3])
    if target is None:
        return body
    out = []
    for line in body:
        if line.endswith("|") and not line.startswith(("V:", "%", "M:", "K:", "w:")):
            out.append(_repair_music_line(line, target))
        else:
            out.append(line)
    return out

NATURAL = dict(zip("CDEFGAB", (0, 2, 4, 5, 7, 9, 11)))
KEYS = {
    **dict(zip(("Cb", "Gb", "Db", "Ab", "Eb", "Bb", "F", "C", "G", "D", "A", "E", "B", "F#", "C#"), range(-7, 8))),
    **dict(zip(("Abm", "Ebm", "Bbm", "Fm", "Cm", "Gm", "Dm", "Am", "Em", "Bm", "F#m", "C#m", "G#m", "D#m", "A#m"), range(-7, 8))),
}


class AbcError(ValueError):
    """Unsupported notation or a failed structural invariant."""


def fail(condition: bool, message: str) -> None:
    if condition:
        raise AbcError(message)


def key_accidentals(key: str) -> dict[str, int]:
    fail(key not in KEYS, f"Unsupported key {key!r}; use a standard major or minor K: field")
    count = KEYS[key]
    result = {letter: 0 for letter in NATURAL}
    for letter in ("FCGDAEB" if count > 0 else "BEADGCF")[:abs(count)]:
        result[letter] = 1 if count > 0 else -1
    return result


def meter_value(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", text)
    fail(match is None, f"Unsupported meter {text!r}; write an explicit fraction")
    n, d = map(int, match.groups())
    fail(d > 1024 or d & (d - 1) != 0, f"Unsupported meter denominator {d}")
    return n, d


@dataclass
class Voice:
    meter: tuple[int, int]
    key: str
    time: Fraction = Fraction(0)
    notes: list = field(default_factory=list)
    bars: list = field(default_factory=list)
    chords: list = field(default_factory=list)
    keys: list = field(default_factory=list)
    pending: tuple | None = None


@dataclass
class Score:
    text: str
    unit: Fraction
    bpm: int
    voices: dict[str, Voice]
    music_lines: dict[int, str]


def parse_bar(body: str, voice: Voice, unit: Fraction, context: str) -> None:
    n, d = voice.meter
    length = Fraction(4 * n, d)
    start = voice.time
    offset = Fraction(0)
    local = {}  # Native exporters propagate accidentals by letter, across octaves.
    if body == "Z":
        fail(voice.pending is not None, f"{context}: tie enters a full-measure rest")
        offset = length
    else:
        cursor = 0
        while cursor < len(body):
            if body[cursor].isspace():
                cursor += 1
                continue
            match = TOKEN.match(body, cursor)
            fail(match is None, f"{context}: unsupported token at {body[cursor:cursor + 24]!r}")
            cursor = match.end()
            chord, key = match.group("chord", "key")
            fail(offset >= length, f"{context}: event after the measure end")
            if chord is not None:
                fail(CHORD.fullmatch(chord) is None, f"{context}: unsupported chord {chord!r}")
                voice.chords.append((start + offset, chord))
                continue
            if key is not None:
                key_accidentals(key)
                voice.key = key
                voice.keys.append((start + offset, key))
                local = {}
                continue
            note, acc, octave, tie = match.group("note", "acc", "oct", "tie")
            units = int(match.group("duration") or "1")
            fail(units not in DURATIONS, f"{context}: unsupported duration {units}; split it into tied supported lengths")
            duration = units * unit * 4
            fail(offset + duration > length, f"{context}: note/rest exceeds meter duration")
            fail("," in octave and "'" in octave, f"{context}: mixed octave marks")
            if note == "z":
                fail(bool(acc or octave or tie), f"{context}: a rest cannot have accidentals, octave marks or ties")
                fail(voice.pending is not None, f"{context}: tie enters a rest")
            else:
                letter = note.upper()
                written = 60 + NATURAL[letter] + (12 if note.islower() else 0)
                written += 12 * (octave.count("'") - octave.count(","))
                alteration = local.get(letter, key_accidentals(voice.key)[letter])
                if acc:
                    alteration = {"=": 0, "_": -1, "__": -2, "^": 1, "^^": 2}[acc]
                    local[letter] = alteration
                pitch = written + alteration
                if voice.pending is not None:
                    old_pitch, old_written = voice.pending
                    # An unmarked continuation retains its tied accidental across
                    # a barline. It does not alter later untied notes in that bar.
                    if not acc and written == old_written:
                        pitch = old_pitch
                    fail(pitch != old_pitch, f"{context}: tie changes pitch from {old_pitch} to {pitch}")
                    voice.notes[-1][2] += duration
                else:
                    fail(not 0 <= pitch <= 127, f"{context}: pitch {pitch} is outside MIDI range")
                    voice.notes.append([start + offset, pitch, duration])
                voice.pending = (pitch, written) if tie else None
            offset += duration
    fail(offset != length, f"{context}: duration {offset} quarter notes != meter duration {length}")
    voice.bars.append((start, length, voice.meter))
    voice.time += length


def parse(text: str) -> Score:
    """Fail closed for unsupported tokens; resolve sounding notes, not token counts."""
    lines = text.splitlines()
    fail(len(lines) < 12, "Incomplete native two-voice ABC")
    fail(lines[0:2] != ["X:1", "T:"], "Expected native X:1 and blank T: header")
    fail(not lines[2].startswith("M:"), "Missing header M:")
    meter = meter_value(lines[2][2:])
    unit_match = re.fullmatch(r"L:1/([1-9][0-9]*)", lines[3])
    fail(unit_match is None, "Expected L:1/<power of two>, usually L:1/32")
    denominator = int(unit_match.group(1))
    fail(denominator > 1024 or denominator & (denominator - 1) != 0, "Unsupported L: denominator")
    unit = Fraction(1, denominator)
    tempo_match = re.fullmatch(r"Q:1/4=([1-9][0-9]*)", lines[4])
    fail(tempo_match is None, "Expected integer quarter-note tempo Q:1/4=<BPM>")
    expected_voices = ['V: Vocal clef=treble name="Vocal Melody" snm="Vocal"',
                       'V: Ins clef=treble name="Ins Melody" snm="Inst."']
    fail(lines[5:7] != expected_voices, "Preserve native Vocal and Ins voice definitions")
    fail(not lines[7].startswith("K:"), "Missing header K:")
    key = lines[7][2:]
    key_accidentals(key)
    voices = {name: Voice(meter, key, keys=[(Fraction(0), key)]) for name in VOICES}
    music_lines = {}
    cursor = 8
    group = 0
    while cursor < len(lines):
        while cursor < len(lines) and lines[cursor].startswith("% "):
            cursor += 1
        fail(cursor == len(lines), "Dangling section comment without music")
        group += 1
        counts = []
        for name in VOICES:
            context = f"group {group}, {name}"
            fail(cursor >= len(lines) or lines[cursor] != f"V: {name}", f"{context}: expected V: {name}")
            cursor += 1
            voice = voices[name]
            fields = set()
            while cursor < len(lines) and lines[cursor].startswith(("M:", "K:")):
                field_name, value = lines[cursor].split(":", 1)
                fail(field_name in fields, f"{context}: duplicate {field_name}: field")
                fields.add(field_name)
                if field_name == "M":
                    voice.meter = meter_value(value)
                else:
                    key_accidentals(value)
                    voice.key = value
                    voice.keys.append((voice.time, value))
                cursor += 1
            fail(cursor >= len(lines), f"{context}: missing music line")
            line = lines[cursor]
            fail(not line.endswith("|"), f"{context}: music line must end with a plain barline")
            music_lines[cursor] = name
            cursor += 1
            bars = []
            for bar in line[:-1].split("|"):
                bar = bar.strip()
                fail(not bar, f"{context}: empty measure or unsupported double/repeat barline")
                rest = re.fullmatch(r"Z([2-4])?", bar)
                if rest:
                    bars.extend(["Z"] * int(rest.group(1) or "1"))
                else:
                    bars.append(bar)
            fail(not 1 <= len(bars) <= 4, f"{context}: expected 1â€“4 measures after expanding Z rests")
            counts.append(len(bars))
            for bar in bars:
                parse_bar(bar, voice, unit, f"{context}, bar {len(voice.bars) + 1}")
        fail(counts[0] != counts[1], f"group {group}: voices have different measure counts")
    for name, voice in voices.items():
        fail(voice.pending is not None, f"{name}: unresolved tie at end of score")
    fail(voices["Ins"].chords != [], "Native chord symbols belong in Vocal, not Ins")
    fail(voices["Vocal"].bars != voices["Ins"].bars, "Voice meter/time grids differ")
    fail(voices["Vocal"].keys != voices["Ins"].keys, "Voice key-change timelines differ")
    return Score(text, unit, int(tempo_match.group(1)), voices, music_lines)
