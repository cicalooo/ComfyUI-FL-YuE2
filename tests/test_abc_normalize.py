import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("yue2_abc_score", ROOT / "abc_score.py")
abc_score = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = abc_score
spec.loader.exec_module(abc_score)


HEADER = """X:1
T:
M:4/4
L:1/16
Q:1/4=88
V: Vocal clef=treble name="Vocal Melody" snm="Vocal"
V: Ins clef=treble name="Ins Melody" snm="Inst."
K:Am
"""

VOCAL = """% verse
V: Vocal
"Am7"z16|"Dm7"z16|"G7"z16|"Cmaj7"z16|
"""

INS = """V: Ins
E2A2c4B2A2G4|F2A2d4c2A2F4|G2B2d4c2B2A4|E2G2c4B2G2E4|
"""


def test_inserts_missing_v_ins():
    raw = HEADER + VOCAL + "E2A2c4B2A2G4|F2A2d4c2A2F4|G2B2d4c2B2A4|E2G2c4B2G2E4|\n"
    fixed = abc_score.normalize_native_abc(raw)
    assert "V: Ins\nE2A2" in fixed
    abc_score.parse(fixed)


def test_aliases_instrumental_and_compact_tags():
    raw = (
        HEADER
        + VOCAL.replace("V: Vocal", "V:Vocal")
        + "V: Instrumental\nE2A2c4B2A2G4|F2A2d4c2A2F4|G2B2d4c2B2A4|E2G2c4B2G2E4|\n"
    )
    fixed = abc_score.normalize_native_abc(raw)
    assert "V: Vocal\n" in fixed and "V: Ins\n" in fixed
    abc_score.parse(fixed)


def test_drops_comment_between_voices():
    raw = HEADER + VOCAL + "% between\n" + INS
    fixed = abc_score.normalize_native_abc(raw)
    assert "% between" not in fixed
    abc_score.parse(fixed)


def test_halves_double_length_bars():
    raw = """X:1
T:
M:4/4
L:1/16
Q:1/4=90
V: Vocal clef=treble name="Vocal Melody" snm="Vocal"
V: Ins clef=treble name="Ins Melody" snm="Inst."
K:C
% verse
V: Vocal
"C"z16|"G"z16|"Am"z16|"F"z16|
V: Ins
C8D8E8F8|G8A8B8c8|A8G8F8E8|D8C8B,8C8|
"""
    fixed = abc_score.normalize_native_abc(raw)
    assert "C4D4E4F4|" in fixed
    abc_score.parse(fixed)


def test_pads_short_bars():
    raw = """X:1
T:
M:4/4
L:1/16
Q:1/4=90
V: Vocal clef=treble name="Vocal Melody" snm="Vocal"
V: Ins clef=treble name="Ins Melody" snm="Inst."
K:C
% verse
V: Vocal
"C"z8|
V: Ins
C4D4|
"""
    fixed = abc_score.normalize_native_abc(raw)
    abc_score.parse(fixed)
