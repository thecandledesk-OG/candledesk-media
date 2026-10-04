#!/usr/bin/env python3
"""Breaking-news Short scripts: lint them, and turn them into a playable/renderable news player.

  script_tools.py lint script.json
      Checks structure (hook → what happened → why it matters → watch next → CTA), length (30–60 s),
      banned hype phrases, sources (2+). Exit code 1 if there are errors.
  script_tools.py lines script.json lines.json
      Writes [{who, vo}] for voice_episode.py.
  script_tools.py player news_player.html script.json out.html [timings.json]
      Builds a LOCAL breaking-news copy of the news player: "BREAKING" label, the scenes, and a ticker
      that lists the sources. The live news player artifact is never edited.

script.json:
{"story_id": "st_...", "date_label": "OCT 8", "headline": "...",
 "sources": [{"name": "SEC", "url": "https://..."}, ...],
 "scenes": [
   {"beat": "hook",           "who": "ledger", "vo": "...", "chunks": ["..","..",".."], "stat": {...}},
   {"beat": "what_happened",  "who": "ledger", ...},
   {"beat": "why_it_matters", "who": "bruno|ursa|ledger", ...},   (one or two scenes: bull and bear read)
   {"beat": "watch_next",     "who": "ursa|moby|ledger", ...},
   {"beat": "cta",            "who": "all", "vo": "...", "chunks": [...], "foot": "NOT FINANCIAL ADVICE"}]}
"""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ORDER = ["hook", "what_happened", "why_it_matters", "watch_next", "cta"]
WPS = 2.55          # cast's speaking rate, words per second (measured on voiced episodes)
SCENE_PAD = 0.7     # lead + tail silence per scene, seconds
HEDGED = re.compile(r"\b(will|is going to|are going to)\s+(rise|fall|rally|crash|pump|dump|soar|surge|tank|go up|go down|hit)\b", re.I)

def load_cfg():
    with open(os.environ.get("BREAKING_CONFIG") or os.path.join(HERE, "config.json")) as f:
        return json.load(f)

def lint(script, cfg=None):
    cfg = cfg or load_cfg()
    errors, warnings = [], []
    scenes = script.get("scenes") or []
    beats = [s.get("beat") for s in scenes]
    collapsed = [b for i, b in enumerate(beats) if i == 0 or b != beats[i - 1]]
    if collapsed != ORDER:
        errors.append(f"beats must run {' → '.join(ORDER)} (why_it_matters may take two scenes); got {' → '.join(map(str, beats))}")
    if beats.count("why_it_matters") > 2:
        errors.append("why_it_matters can take at most two scenes")
    words = 0
    for i, s in enumerate(scenes, 1):
        vo = s.get("vo", "")
        n = len(vo.split())
        words += n
        low = vo.lower()
        for p in cfg["BANNED_PHRASES"]:
            if p in low:
                errors.append(f"scene {i}: banned hype phrase “{p}”")
        for m in HEDGED.finditer(vo):
            errors.append(f"scene {i}: price prediction stated as fact (“{m.group(0)}”) — say what traders are watching instead")
        if re.search(r"\d|[$%€£]", vo):
            errors.append(f"scene {i}: spell numbers and symbols out in the voiceover")
        if n > 40:
            errors.append(f"scene {i}: {n} words — keep each line under 40")
        if s.get("who") not in ("ledger", "bruno", "ursa", "moby", "all"):
            errors.append(f"scene {i}: unknown character {s.get('who')!r}")
        ch = s.get("chunks") or []
        if len(ch) != 3:
            errors.append(f"scene {i}: needs exactly 3 caption chunks")
        for c in ch:
            plain = c.replace("*", "")
            if len(plain.split()) > 4 or len(plain) > 24:
                errors.append(f"scene {i}: caption “{plain}” is too long (max 4 words / 24 characters)")
        if "!" in vo and s.get("who") != "bruno":
            warnings.append(f"scene {i}: exclamation outside Bruno's lines — keep the tone factual")
    secs = words / WPS + SCENE_PAD * len(scenes)
    if not (cfg["SCRIPT_WORDS_MIN"] <= words <= cfg["SCRIPT_WORDS_MAX"]) or secs > cfg["SCRIPT_MAX_SECONDS"]:
        errors.append(f"{words} words ≈ {secs:.0f} s; aim for {cfg['SCRIPT_WORDS_MIN']}–{cfg['SCRIPT_WORDS_MAX']} words (30–60 s)")
    srcs = script.get("sources") or []
    if len(srcs) < 2:
        errors.append("list at least two sources (they are shown in the ticker)")
    if scenes and scenes[-1].get("who") != "all":
        errors.append("the CTA scene is the full cast (who: all)")
    if scenes and "not financial advice" not in (scenes[-1].get("foot", "") + scenes[-1].get("vo", "")).lower():
        errors.append("CTA scene needs foot: 'NOT FINANCIAL ADVICE'")
    return {"ok": not errors, "words": words, "est_seconds": round(secs), "errors": errors, "warnings": warnings}

def js_str(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ") + "'"

def js_obj(d):
    parts = []
    for k, v in d.items():
        if isinstance(v, dict):
            parts.append(f"{k}:{js_obj(v)}")
        elif isinstance(v, list):
            parts.append(f"{k}:[" + ",".join(js_str(x) for x in v) + "]")
        elif isinstance(v, bool):
            parts.append(f"{k}:{'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            parts.append(f"{k}:{v}")
        else:
            parts.append(f"{k}:{js_str(v)}")
    return "{" + ",".join(parts) + "}"

def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def build_player(html, script, timings=None):
    scenes = []
    for i, s in enumerate(script["scenes"]):
        o = {"who": s["who"]}
        if timings:
            o.update({k: timings[i][k] for k in ("dur", "talkFrom", "talk")})
        o["vo"] = s["vo"]
        o["chunks"] = s["chunks"]
        if s.get("stat"):
            o["stat"] = s["stat"]
        if s.get("foot"):
            o["foot"] = s["foot"]
        scenes.append(" " + js_obj(o))
    out, n = re.subn(r"const scenes=\[.*?\n\];", lambda m: "const scenes=[\n" + ",\n".join(scenes) + "\n];", html, count=1, flags=re.S)
    if n != 1:
        raise SystemExit("could not find the scenes array in the news player")
    label = f"BREAKING · {script.get('date_label', '').upper()}".strip(" ·")
    out = re.sub(r'(<span class="live">)[^<]*(</span>)', lambda m: m.group(1) + esc(label) + m.group(2), out, count=1)
    names = " · ".join(esc(s["name"]) for s in script["sources"])
    track = f'<span>SOURCES: {names}</span><span>{esc(script.get("headline", ""))}</span><span>NOT FINANCIAL ADVICE</span>'
    out = re.sub(r'(<div class="track">).*?(</div>)', lambda m: m.group(1) + "\n        " + track + "\n      " + m.group(2), out, count=1, flags=re.S)
    links = " · ".join(f'<a href="{esc(s["url"])}" target="_blank" rel="noopener">{esc(s["name"])}</a>' for s in script["sources"])
    out = re.sub(r'(<p class="src">).*?(</p>)', lambda m: m.group(1) + "Breaking-news sources: " + links + m.group(2), out, count=1, flags=re.S)
    out = re.sub(r'(<p class="hint">).*?(</p>)', lambda m: m.group(1) + "Breaking-news Short: " + esc(script.get("headline", "")) + m.group(2), out, count=1, flags=re.S)
    return out

def main(a):
    if not a:
        print(__doc__); return
    if a[0] == "lint":
        r = lint(json.load(open(a[1])))
        print(json.dumps(r, indent=1, ensure_ascii=False))
        sys.exit(0 if r["ok"] else 1)
    if a[0] == "lines":
        sc = json.load(open(a[1]))["scenes"]
        json.dump([{"who": s["who"], "vo": s["vo"]} for s in sc], open(a[2], "w"), indent=1)
        return
    if a[0] == "player":
        tim = json.load(open(a[4])) if len(a) > 4 else None
        open(a[3], "w").write(build_player(open(a[1]).read(), json.load(open(a[2])), tim))
        return
    print(__doc__); sys.exit(1)

if __name__ == "__main__":
    main(sys.argv[1:])
