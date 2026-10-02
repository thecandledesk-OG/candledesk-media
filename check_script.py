#!/usr/bin/env python3
"""Daily script check for The Candle Desk, run BEFORE voicing.

Usage: python3 check_script.py <script.json> [pron.json]
  script.json: a list of scenes [{who, vo, chunks?, gfx?}], or a lesson file {"short":{scenes}, "long":{scenes}}.
  pron.json:   the shared pronunciation list (default: ./pron.json).

Prints, for each scene, anything worth a human look:
  SAY   unusual words / acronyms -> how they will be respelled -> phonemes the voice engine will use
  DIGIT digits or symbols left in the spoken line (the engine can misread "$1.5B", "24h", "%")
  CAP   on-screen caption pieces that are too long to fit
  WARN  warning-card text over 24 characters
  LONG  spoken lines over 45 words
Ends with a summary. Exit code 0 always; the reviewer decides what to fix.
Needs: kokoro-onnx (for phonemes) and wordfreq (pip install --break-system-packages wordfreq).
"""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))

def load_pron(path):
    try:
        return json.load(open(path))
    except Exception:
        return {}

def make_pronounce(pron):
    if not pron:
        return lambda t: t
    rx = re.compile(r"\b(" + "|".join(sorted(map(re.escape, pron), key=len, reverse=True)) + r")(?=('s)?\b)")
    return lambda t: rx.sub(lambda m: pron[m.group(1)], t)

def scenes_of(data):
    if isinstance(data, list):
        return [("episode", data)]
    out = []
    for k in ("short", "long"):
        if isinstance(data.get(k), dict) and data[k].get("scenes"):
            out.append((k, data[k]["scenes"]))
    return out

def main(script_path, pron_path):
    from wordfreq import zipf_frequency
    phon = None
    try:
        from kokoro_onnx import Kokoro
        md = os.path.join(HERE, "tts")
        k = Kokoro(os.path.join(md, "kokoro-v1.0.onnx"), os.path.join(md, "voices-v1.0.bin"))
        phon = lambda w: k.tokenizer.phonemize(w, "en-us")
    except Exception as e:
        print(f"(phonemes unavailable: {e}; listing terms only)")
    pron = load_pron(pron_path)
    pronounce = make_pronounce(pron)
    data = json.load(open(script_path))
    counts = {"SAY": 0, "DIGIT": 0, "CAP": 0, "WARN": 0, "LONG": 0}
    seen = set()
    for label, scenes in scenes_of(data):
        print(f"\n=== {label} ===")
        for i, s in enumerate(scenes, 1):
            vo = s.get("vo", "")
            notes = []
            for w in re.findall(r"[A-Za-z][A-Za-z'\-]*", vo):
                base = re.sub(r"'s$", "", w)
                rare = zipf_frequency(base.lower(), "en") < 3.0
                acro = len(base) >= 2 and (base.isupper() or (any(c.isupper() for c in base[1:]) and not base.isupper()))
                if (rare or acro) and base not in seen:
                    seen.add(base)
                    said = pronounce(base)
                    notes.append(f"  SAY   {base:16} -> {said:18} /{phon(said) if phon else '?'}/" + ("   (in pron list)" if base in pron else ""))
                    counts["SAY"] += 1
            if re.search(r"[0-9$%€£#&@+=/]", vo):
                notes.append(f"  DIGIT spoken line has digits/symbols: {re.findall(r'[^ ]*[0-9$%€£#&@+=/][^ ]*', vo)}")
                counts["DIGIT"] += 1
            for c in s.get("chunks", []):
                plain = c.replace("*", "")
                if len(plain.split()) > 4 or len(plain) > 24:
                    notes.append(f"  CAP   caption too long ({len(plain)} chars): {plain!r}")
                    counts["CAP"] += 1
            g = s.get("gfx") or {}
            for it in g.get("items", []) if g.get("type") == "warn" else []:
                if len(it.get("t", "")) > 24:
                    notes.append(f"  WARN  warning text over 24 chars: {it['t']!r}")
                    counts["WARN"] += 1
            if len(vo.split()) > 45:
                notes.append(f"  LONG  {len(vo.split())} words")
                counts["LONG"] += 1
            if notes:
                print(f"[{i}] {s.get('who','?')}: {vo[:70]}{'…' if len(vo) > 70 else ''}")
                print("\n".join(notes))
    print("\nSUMMARY " + " · ".join(f"{k} {v}" for k, v in counts.items()))
    print("Review every SAY line: if the phonemes would not sound like a crypto native saying it, add the word to pron.json "
          "(word -> plain-English respelling, test with the phonemes shown), and fix every DIGIT/CAP/WARN/LONG item in the script, then re-run.")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "pron.json")
