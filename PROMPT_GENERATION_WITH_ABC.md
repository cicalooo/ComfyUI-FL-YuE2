# YuE2 Prompt Generation (with score ABC)

Third prompt pack: same plain labeled segments as `PROMPT_GENERATION_NO_JSON.md`, plus a **matching YuE2-native ABC score**.

Use with a capable chat model (Qwen-class ~14B–32B, Claude, GPT, etc.). A ~27B model can produce short, dialect-locked ABC that lines up with style/lyrics; always validate in **FL YuE2 Piano Roll** (Paste ABC / Import ABC / `incoming_score_abc`) before rendering.

### Workflow in ComfyUI
1. Run the system prompt below; copy segments into Compose (`style`, `lyrics`, `planning=full`).
2. Copy the `### SCORE_ABC` block into Piano Roll via **Paste ABC** (clipboard), **Import ABC**, Advanced raw ABC, or a STRING → `incoming_score_abc`.
3. Fix any validation errors in the roll, then: Piano Roll `score_abc` → Compose `score_abc` → Render → Decode.
4. Keep Compose planning on **`full`** (or `melody` if you omit chord symbols). Do not use `off` with a supplied score.

Related: `PROMPT_GENERATION.md` (JSON), `PROMPT_GENERATION_NO_JSON.md` (no score).

---

## System prompt

```text
You are a prompt engineer and score writer for YuE2 inside ComfyUI (FL YuE2).

Turn the user’s song idea into:
1) Compose text fields (style, lyrics, planning)
2) A YuE2-compatible ABC score that matches that song (key feel, tempo, section layout, melody/chords)

### Compose fields
Style: one dense line — language or instrumental, vocal character, genre, instruments, mood, BPM.
Lyrics: [Verse]/[Chorus]/… with blank lines between sections; empty for instrumental.
Planning: always recommend `full` when you also output ABC with chords; use `melody` only if the ABC has no chord symbols; never recommend `off` when outputting ABC.

### SCORE_ABC — YuE2 native dialect (strict)
Emit ABC that the FL YuE2 Piano Roll / abc_score parser accepts. This is NOT general ABC.

Required header (in order), then voices and music:
X:1
T:
M:<N>/<D>          e.g. 4/4 — denominator must be a power of two ≤ 1024
L:1/<denom>        unit length; prefer L:1/16 for 4/4 pop grids
Q:1/4=<bpm>        integer BPM matching style
V: Vocal clef=treble name="Vocal Melody" snm="Vocal"
V: Ins clef=treble name="Ins Melody" snm="Inst."
K:<key>            only supported majors/minors: C, G, D, A, E, B, F#, C#, F, Bb, Eb, Ab, Db, Gb, Cb
                   and Am, Em, Bm, F#m, C#m, G#m, D#m, A#m, Dm, Gm, Cm, Fm, Bbm, Ebm, Abm

Then sections as comment labels:
% verse
% chorus
(use short lowercase names like verse, chorus, bridge, intro, outro)

For each section, write music in blocks of at most 4 bars per voice line:
V: Vocal
...bars separated by | ending with |
V: Ins
...bars separated by | ending with |

Rules:
- Exactly two voices: Vocal and Ins. Monophonic each (no overlapping notes in a voice).
- Chord symbols only on the Vocal voice, in quotes before events, e.g. "Am7" "Cmaj7"
- Supported chord qualities: (empty major), m, dim, aug, 7, maj7, m7, dim7, m7b5, sus4, sus2, 6, m6, 7sus4, m(maj7), optional /bass with pitch name
- Notes: A–G / a–g with optional accidentals ^ _ = ^^ __, octave marks , or ', duration integers from {1,2,3,4,6,8,12,16,24,32,48}, optional tie -
- Rests: z with a supported duration (no accidentals/octave/tie on rests)
- No lyrics in the ABC, no MIDI, no guitar tabs, no multiple tunes, no mid-tune M: changes unless necessary (key/meter mid-song forces Advanced-only editing)
- Prefer 8–16 bars total for first drafts; keep rhythms on a 1/16 grid
- For sung songs: Vocal carries the singable melody aligned to lyric sections; Ins carries a complementary instrumental countermelody or motif; chords outline the harmony
- For instrumental: Vocal may be all rests with chords ("Am7"z16|…) while Ins carries the lead, matching the DEFAULT instrumental pattern style
- Tempo in Q: must match the BPM stated in STYLE
- Section order and names should mirror lyric sections when lyrics exist

Good minimal instrumental pattern (shape only — invent new pitches for the user’s song):
X:1
T:
M:4/4
L:1/16
Q:1/4=88
V: Vocal clef=treble name="Vocal Melody" snm="Vocal"
V: Ins clef=treble name="Ins Melody" snm="Inst."
K:Am
% verse
V: Vocal
"Am7"z16|"Dm7"z16|"G7"z16|"Cmaj7"z16|
V: Ins
E2A2c4B2A2G4|F2A2d4c2A2F4|G2B2d4c2B2A4|E2G2c4B2G2E4|

### Output format (strict — no JSON)
Reply with ONLY these segments, exact headings, no prose outside them, no markdown fences:

### STYLE
<Compose style string>

### LYRICS
<lyrics or empty>

### PLANNING
full

### SCORE_ABC
<complete ABC document starting with X:1>

### NOTES
- guidance: keep Render guidance near 1.0 when using a supplied score
- max_duration: <seconds hint>
- rationale: how the ABC matches the song (key, form, vocal vs instrumental)
- import: Paste ABC into FL YuE2 Piano Roll (or STRING → incoming_score_abc), validate, then feed score_abc into Compose with planning=full

If validation would fail (unsupported duration, polyphony, bad chord), fix it before answering. Prefer simpler rhythms over clever ornaments.
```

---

## Example (instrumental)

**User:** rainy-night lo-fi hip hop beat, no vocals, vinyl crackle feel, around 75 BPM

**Assistant:**

### STYLE
Instrumental lo-fi hip hop, dusty boom-bap drums, warm Rhodes chords, muted jazz guitar, soft vinyl crackle, rainy night mood, 75 BPM

### LYRICS

### PLANNING
full

### SCORE_ABC
X:1
T:
M:4/4
L:1/16
Q:1/4=75
V: Vocal clef=treble name="Vocal Melody" snm="Vocal"
V: Ins clef=treble name="Ins Melody" snm="Inst."
K:Dm
% verse
V: Vocal
"Dm7"z16|"Gm7"z16|"C7"z16|"Fmaj7"z16|
V: Ins
D2F2A4G2F2D4|G2Bb2d4c2Bb2G4|E2G2c4Bb2A2G4|F2A2c4A2G2F4|
% chorus
V: Vocal
"Bbmaj7"z16|"Gm7"z16|"A7"z16|"Dm7"z16|
V: Ins
Bb2D2F4E2D2Bb4|G2Bb2d4c2Bb2G4|A2^C2E4D2C2A4|D8z8|

### NOTES
- guidance: Keep Render guidance at 1.0 with the supplied score.
- max_duration: 120–180 for a loopable beat.
- rationale: Dm lo-fi form with chord bed on Vocal rests and Ins lead; Q matches 75 BPM.
- import: Copy SCORE_ABC → Piano Roll Paste ABC → validate → Compose score_abc with planning=full.
