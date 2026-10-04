#!/usr/bin/env python3
"""The Candle Desk — Breaking News Engine (rules + records + audit log).

The AI does the searching and reading; this file makes every decision that has to be
consistent and auditable: verification, classification checks, duplicate grouping,
rate limits, publish gates and logging. Nothing here posts anything; it tells the
caller what to do and records why.

Commands (run from any directory):
  engine.py status [--now ISO]                 config, today's counters, recent stories (compare against these before ingesting)
  engine.py ingest candidates.json [--now ISO] [--run RUN_ID]
                                               dedupe + verify + classify each candidate, store, log; prints decisions
  engine.py plan [--now ISO] [--run RUN_ID]    decide what gets a Short now (priority, limits, gates); prints actions
  engine.py mark STORY_ID key=value ...        record progress (content_created=true, publication_status=SCHEDULED, ...)
  engine.py log STORY_ID STEP "detail"         add an audit entry (script_generated, video_generated, publishing, error...)
  engine.py covered-in-daily stories.json      record what today's daily episode covered (once per day)
  engine.py daily-candidates [--mark-used ID,ID] [--now ISO]
                                               NORMAL stories waiting for the morning episode
  engine.py report [--since ISO] [--run RUN_ID] human-readable audit

State lives in ./state next to this file (override with BREAKING_STATE_DIR).
"""
import json, os, re, sys, uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ET = ZoneInfo("America/New_York")
LEVELS = ["NORMAL", "IMPORTANT", "BREAKING"]
RANK = {l: i for i, l in enumerate(LEVELS)}

# ---------------------------------------------------------------- storage

def state_dir():
    d = os.environ.get("BREAKING_STATE_DIR") or os.path.join(HERE, "state")
    os.makedirs(d, exist_ok=True)
    return d

def load_config(path=None):
    path = path or os.environ.get("BREAKING_CONFIG") or os.path.join(HERE, "config.json")
    with open(path) as f:
        return json.load(f)

def load_stories():
    p = os.path.join(state_dir(), "stories.json")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return json.load(f)

def save_stories(stories):
    p = os.path.join(state_dir(), "stories.json")
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(stories, f, indent=1, ensure_ascii=False)
    os.replace(tmp, p)

def read_log():
    p = os.path.join(state_dir(), "log.jsonl")
    if not os.path.exists(p):
        return []
    out = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out

class Logger:
    def __init__(self, run_id, now):
        self.run_id, self.now = run_id, now
    def __call__(self, story_id, step, detail, **data):
        e = {"ts": iso(self.now), "run": self.run_id, "story": story_id, "step": step, "detail": detail}
        if data:
            e["data"] = data
        with open(os.path.join(state_dir(), "log.jsonl"), "a") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return e

# ---------------------------------------------------------------- time helpers

def parse_time(s):
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    s = s.strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        try:
            d = datetime.fromisoformat(s[:10])
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=ET)

def iso(d):
    return d.astimezone(ET).isoformat(timespec="minutes")

def et_day(d):
    return d.astimezone(ET).date().isoformat()

def get_now(args):
    if "--now" in args:
        return parse_time(args[args.index("--now") + 1])
    return datetime.now(ET)

# ---------------------------------------------------------------- sources & verification

def domain_of(url):
    u = urlparse(url if "://" in url else "https://" + url)
    host = (u.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host, (u.path or "/")

def registrable(host):
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "gov", "ac", "net") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])

def _match_list(host, path, entries):
    for e in entries:
        ed, _, ep = e.partition("/")
        if host == ed or host.endswith("." + ed):
            if not ep or path.lstrip("/").startswith(ep):
                return True
    return False

def classify_source(src, cfg):
    """Returns (category, note). category: social | primary | secondary | other."""
    host, path = domain_of(src.get("url", ""))
    kind = (src.get("kind") or "other").lower()
    if kind == "social" or _match_list(host, path, cfg["SOCIAL_DOMAINS"]):
        return "social", "social/community post — can trigger investigation, never verifies"
    if host.endswith(".gov") or _match_list(host, path, cfg["PRIMARY_DOMAINS"]):
        return "primary", "primary (known official domain)"
    if _match_list(host, path, cfg["CREDIBLE_SECONDARY_DOMAINS"]):
        return "secondary", "credible news organization"
    if kind == "primary":
        return "primary", "primary (claimed official source; domain not on the known list)"
    return "other", "not on the credible list — logged as evidence only"

def verify(sources, cfg):
    """Apply the corroboration rule. Returns a verification dict (also stored on the story)."""
    checked, groups, anchor, has_primary = [], {}, False, False
    for s in sources:
        cat, note = classify_source(s, cfg)
        host, _ = domain_of(s.get("url", ""))
        counts = cat in ("primary", "secondary") and s.get("checked", False) and s.get("confirms", False)
        why = note
        if cat in ("primary", "secondary") and not s.get("checked", False):
            why += "; not opened, so it doesn't count"
        elif cat in ("primary", "secondary") and not s.get("confirms", False):
            why += "; opened but does not confirm the claim"
        checked.append({"name": s.get("name") or host, "url": s.get("url"), "category": cat,
                        "counts": counts, "note": why})
        if counts:
            # Independence: one vote per publisher. Syndicated copies share an "origin".
            key = (s.get("origin") or registrable(host)).lower()
            groups.setdefault(key, []).append(s.get("url"))
            if cat == "primary":
                has_primary = True
            if "claimed" not in note:
                anchor = True
    n = len(groups)
    verified = n >= cfg["MIN_INDEPENDENT_SOURCES"] and anchor
    if verified:
        reason = f"{n} independent credible sources confirm it" + (" (incl. a primary source)" if has_primary else " (no primary source yet)")
    elif n >= cfg["MIN_INDEPENDENT_SOURCES"] and not anchor:
        reason = "sources only from domains not on the known credible list; needs one known outlet or official domain"
    else:
        reason = f"only {n} independent credible source(s) confirm it; need {cfg['MIN_INDEPENDENT_SOURCES']}"
    score = min(1.0, 0.4 * min(n, 2) + (0.2 if has_primary else 0.0))
    return {"status": "VERIFIED" if verified else "UNVERIFIED", "independent_sources": n,
            "has_primary": has_primary, "score": round(score, 2), "reason": reason, "sources_checked": checked}

# ---------------------------------------------------------------- classification checks

def rule_level(c, cfg, now):
    """Deterministic checks on top of the AI's proposed level. Returns (level, [notes])."""
    proposed = c.get("proposed_level", "NORMAL").upper()
    if proposed not in RANK:
        proposed = "NORMAL"
    level, notes = proposed, [f"AI proposed {proposed}: {c.get('reason', '').strip()}"]
    m = c.get("metrics") or {}
    et = (c.get("event_type") or "other").lower()
    assets = [a.upper() for a in c.get("assets") or []]

    if et == "market_move" and m.get("pct_move_24h") is not None:
        pct = abs(float(m["pct_move_24h"]))
        th = cfg["MARKET_MOVE_THRESHOLDS_PCT"]
        key = next((a for a in assets if a in th), "DEFAULT")
        lo, hi = th[key]
        lvl = "BREAKING" if pct >= hi else "IMPORTANT" if pct >= lo else "NORMAL"
        notes.append(f"price rule: {key} moved {pct:.1f}% in 24h (thresholds {lo}%/{hi}%) -> {lvl}")
        level = lvl
    if et == "hack_exploit" and m.get("loss_usd") is not None:
        lo, hi = cfg["HACK_LOSS_USD_THRESHOLDS"]
        loss = float(m["loss_usd"])
        lvl = "BREAKING" if loss >= hi else "IMPORTANT" if loss >= lo else "NORMAL"
        notes.append(f"hack rule: about ${loss/1e6:,.0f}M lost (thresholds ${lo/1e6:,.0f}M/${hi/1e6:,.0f}M) -> {lvl}")
        level = lvl
    if et == "outage" and m.get("outage_minutes") is not None:
        major = any(a in cfg["MAJOR_ASSETS"] for a in assets)
        mins = float(m["outage_minutes"])
        lvl = "BREAKING" if (major and mins >= cfg["OUTAGE_BREAKING_MINUTES"]) else "IMPORTANT"
        notes.append(f"outage rule: {mins:.0f} min, major network={major} -> {lvl}")
        level = lvl

    # Something announced for the future hasn't happened yet: it's a lead for the daily show.
    ev = parse_time(c.get("event_time"))
    if c.get("scheduled_event") and ev and ev > now and RANK[level] > 0:
        notes.append(f"expected event (due {iso(ev)}) has not happened yet -> NORMAL until it does")
        level = "NORMAL"
    # Old news is not breaking news.
    first = earliest_time(c)
    if first and RANK[level] > 0 and now - first > timedelta(hours=cfg["MAX_STORY_AGE_HOURS"]):
        notes.append(f"first reported {iso(first)}, older than {cfg['MAX_STORY_AGE_HOURS']}h -> NORMAL (daily show)")
        level = "NORMAL"
    if level != proposed:
        notes.append(f"final level {level} (changed from AI's {proposed})")
    return level, notes

def earliest_time(c):
    ts = [parse_time(s.get("published")) for s in c.get("sources") or []]
    ts = [t for t in ts if t]
    ev = parse_time(c.get("event_time"))
    if ev and not c.get("scheduled_event"):
        ts.append(ev)
    return min(ts) if ts else None

# ---------------------------------------------------------------- duplicates

STOP = set("""a an the and or of to in on for with by at from as is are was were be been it its this that these those
new news says said report reports crypto cryptocurrency token tokens coin coins market markets price prices today after
over amid into than more up down just will could would may can has have had not no""".split())

def tokens(text):
    # crude stemming: the first 6 letters make activate/activates/activation the same word
    return {w[:6] for w in re.findall(r"[a-z0-9$%]+", (text or "").lower()) if w not in STOP and len(w) > 1}

ID_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9_\-]*\b")

def identifiers(text):
    """Distinctive names like PermissionDelegationV1_1, BatchV1_1, XLS-75, EIP-7702."""
    return {m.group(0).lower() for m in ID_RE.finditer(text or "")
            if len(m.group(0)) >= 5 and m.group(0).lower() not in GENERIC_IDS}

# Common terms with digits that name a category, not one specific event.
GENERIC_IDS = {"layer-1", "layer-2", "layer1", "layer2", "erc-20", "erc20", "erc-721", "erc721", "erc-1155", "web3",
               "covid-19", "bep-20", "bep20", "trc-20", "trc20", "spl-20", "eip-1559", "x402", "top-10", "top-20",
               "24-hour", "7-day", "30-day", "s&p500", "sp500", "g20", "brc-20", "brc20", "rwa"}

def story_texts(s):
    yield s.get("headline", "") + " " + s.get("summary", "")
    for u in s.get("updates", []):
        yield u.get("headline", "") + " " + u.get("summary", "")

def similarity(a, b):
    ta = tokens(a.get("headline", "") + " " + a.get("summary", ""))
    tb = tokens(b.get("headline", "") + " " + b.get("summary", ""))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

def find_duplicate(c, stories, cfg, now):
    """Returns (story, reason) for the event this candidate belongs to, or (None, reason)."""
    if c.get("related_to"):
        for s in stories:
            if s["id"] == c["related_to"]:
                return s, f"explicitly linked to {s['id']}"
    urls = {x.get("url") for x in c.get("sources") or []}
    ca = {a.upper() for a in c.get("assets") or []}
    best, best_sim = None, 0.0
    for s in stories:
        anchor = max(t for t in (parse_time(s["detected_at"]), parse_time(s.get("event_time"))) if t)
        if now - anchor > timedelta(hours=cfg["DEDUPE_WINDOW_HOURS"]):
            continue
        if urls & set(s.get("source_urls", [])):
            return s, f"shares a source URL with {s['id']}"
        sa = {a.upper() for a in s.get("assets", [])}
        if ca and sa and not (ca & sa):
            continue
        ctext = c.get("headline", "") + " " + c.get("summary", "")
        shared = identifiers(ctext) & set().union(*(identifiers(t) for t in story_texts(s)))
        if shared:
            return s, f"same event as {s['id']} (both name {', '.join(sorted(shared))}, same asset)"
        sim = max(similarity({"headline": ctext}, {"headline": t}) for t in story_texts(s))
        need = cfg["DEDUPE_SIMILARITY"] * (0.75 if c.get("event_type") and c.get("event_type") == s.get("event_type") else 1.0)
        if sim >= need and sim > best_sim:
            best, best_sim = s, sim
    if best:
        return best, f"same event as {best['id']} (headline similarity {best_sim:.2f}, same asset)"
    return None, f"no matching event in the last {cfg['DEDUPE_WINDOW_HOURS']}h"

# ---------------------------------------------------------------- ingest

def covered(s):
    """Already got (or, in dry run, would have got) its own Short."""
    return s["content_created"] or s["publication_status"] in ("WOULD_PUBLISH_DRY_RUN", "READY_TO_PUBLISH", "READY_FOR_APPROVAL")

def new_story(c, now, level, notes, ver):
    return {
        "id": "st_" + now.astimezone(ET).strftime("%Y%m%d") + "_" + uuid.uuid4().hex[:6],
        "headline": c.get("headline", "").strip(),
        "summary": c.get("summary", "").strip(),
        "detected_at": iso(now),
        "active_since": iso(now),
        "classification": level,
        "proposed_level": c.get("proposed_level", "NORMAL").upper(),
        "confidence": float(c.get("confidence", 0.5)),
        "assets": [a.upper() for a in c.get("assets") or []],
        "event_type": c.get("event_type", "other"),
        "event_time": c.get("event_time"),
        "scheduled_event": bool(c.get("scheduled_event")),
        "metrics": c.get("metrics") or {},
        "emergency": bool(c.get("emergency")),
        "source_urls": [s.get("url") for s in c.get("sources") or []],
        "source_names": [s.get("name") for s in c.get("sources") or []],
        "sources": c.get("sources") or [],
        "gate_sources": list(c.get("sources") or []),
        "verification_status": ver["status"],
        "verification": ver,
        "classification_reason": notes,
        "market_significance": c.get("significance", "").strip(),
        "trigger": c.get("trigger", ""),
        "content_created": False,
        "publication_status": "QUEUED_FOR_DAILY" if level == "NORMAL" else "PENDING",
        "related_story_ids": [],
        "updates": [],
        "posts": [],
        "needs_followup": False,
    }

def ingest(candidates, cfg, now, run_id):
    stories = load_stories()
    log = Logger(run_id, now)
    decisions = []
    for c in candidates:
        level, notes = rule_level(c, cfg, now)
        ver = verify(c.get("sources") or [], cfg)
        dup, dup_reason = find_duplicate(c, stories, cfg, now)
        if dup is None:
            s = new_story(c, now, level, notes, ver)
            stories.append(s)
            log(s["id"], "discovered", s["headline"], trigger=c.get("trigger", ""), assets=s["assets"])
            log(s["id"], "sources_checked", f"{len(ver['sources_checked'])} sources", sources=ver["sources_checked"])
            log(s["id"], "verification", f"{ver['status']}: {ver['reason']}")
            log(s["id"], "classification", f"{level} (confidence {s['confidence']:.2f})", reasons=notes)
            log(s["id"], "duplicate_check", "new event — " + dup_reason)
            decisions.append({"story": s["id"], "result": "NEW", "level": level, "verification": ver["status"],
                              "why": notes, "duplicate": dup_reason})
            continue
        # Same underlying event: merge evidence instead of making another story/video.
        s = dup
        old_ver, old_level, was_covered = s["verification_status"], s["classification"], covered(s)
        # Judge the new report on its own timing, so a fresh development of an older event isn't dismissed as old news.
        new_level, new_notes = rule_level(c, cfg, now)
        dev = bool(c.get("new_development"))
        upgrade = dev and RANK[new_level] > RANK[old_level]
        followup = dev and not upgrade and new_level == "BREAKING" and was_covered
        added = 0
        for src in c.get("sources") or []:
            if src.get("url") not in s["source_urls"]:
                s["sources"].append(src)
                s["source_urls"].append(src.get("url"))
                s["source_names"].append(src.get("name"))
                added += 1
        # The publish gate checks the sources for the CURRENT claim: a new development must be
        # corroborated on its own, not by the older reports about the same event.
        if upgrade or followup:
            s["gate_sources"] = list(c.get("sources") or [])
        else:
            gs = s.setdefault("gate_sources", list(s["sources"]))
            gs += [x for x in c.get("sources") or [] if x.get("url") not in {g.get("url") for g in gs}]
        s["verification"] = verify(s["gate_sources"], cfg)
        s["verification_status"] = s["verification"]["status"]
        s["updates"].append({"at": iso(now), "headline": c.get("headline", ""), "summary": c.get("summary", ""), "new_development": dev,
                             "level": new_level, "sources_added": added, "verification": s["verification_status"]})
        log(s["id"], "duplicate_check", f"merged into existing event — {dup_reason}; {added} new source(s)",
            headline=c.get("headline", ""))
        if s["verification_status"] != old_ver or upgrade or followup:
            log(s["id"], "verification", f"{old_ver} -> {s['verification_status']}: {s['verification']['reason']}",
                sources=s["verification"]["sources_checked"])
        if s["verification_status"] == "VERIFIED" and s["publication_status"] == "HUMAN_REVIEW":
            s["publication_status"] = "PENDING"
        result = "MERGED"
        if upgrade:
            s["classification"] = new_level
            s["classification_reason"] += new_notes
            s["confidence"] = float(c.get("confidence", s["confidence"]))
            s["needs_followup"] = was_covered
            s["active_since"] = iso(now)
            if s["publication_status"] not in ("PENDING", "BLOCKED_UNVERIFIED", "HELD_RATE_LIMIT"):
                s["publication_status"] = "PENDING"
            result = "UPGRADED"
            log(s["id"], "classification", f"new development upgrades {old_level} -> {new_level}", reasons=new_notes)
        elif followup:
            s["needs_followup"] = True
            s["publication_status"] = "PENDING"
            s["active_since"] = iso(now)
            s["confidence"] = float(c.get("confidence", s["confidence"]))
            result = "FOLLOWUP"
            log(s["id"], "classification", "new BREAKING development on an event we already covered -> follow-up Short considered",
                reasons=new_notes)
        elif dev:
            log(s["id"], "classification", f"new development noted; stays {old_level}, no extra video", reasons=new_notes)
        elif s["verification_status"] == "VERIFIED" and old_ver != "VERIFIED" and RANK[s["classification"]] > 0:
            result = "NOW_VERIFIED"
        merged_notes = new_notes
        decisions.append({"story": s["id"], "result": result, "level": s["classification"],
                          "verification": s["verification_status"], "duplicate": dup_reason,
                          "why": merged_notes if dev else ["same event, evidence merged"]})
    save_stories(stories)
    return decisions

# ---------------------------------------------------------------- planning

def extra_posts_today(now, log_entries):
    """Extra Shorts already committed today (real or simulated), from the audit log."""
    out = []
    for e in log_entries:
        if e.get("step") == "plan" and e.get("data", {}).get("action") in ("PUBLISH", "DRAFT_FOR_APPROVAL", "WOULD_PUBLISH"):
            when = parse_time(e["data"].get("due_at")) or parse_time(e["ts"])
            if et_day(when) == et_day(now):
                out.append({"level": e["data"].get("level"), "at": when, "story": e.get("story")})
    return out

def slot_conflict(due, cfg):
    d = due.astimezone(ET)
    for hhmm in cfg["SCHEDULED_SLOTS"]:
        h, m = map(int, hhmm.split(":"))
        slot = d.replace(hour=h, minute=m, second=0, microsecond=0)
        if abs((d - slot).total_seconds()) < cfg["QUIET_MINUTES_AROUND_SLOTS"] * 60:
            return slot + timedelta(minutes=cfg["QUIET_MINUTES_AROUND_SLOTS"])
    return None

def plan(cfg, now, run_id):
    stories = load_stories()
    log = Logger(run_id, now)
    prior = extra_posts_today(now, read_log())
    actions = []
    mode = "DRY_RUN" if cfg["DRY_RUN"] else "LIVE"
    todo = [s for s in stories if RANK[s["classification"]] > 0 and s["publication_status"] in
            ("PENDING", "HELD_RATE_LIMIT", "HELD_SPACING", "AWAITING_VERIFICATION", "BLOCKED_UNVERIFIED")]
    todo.sort(key=lambda s: (-RANK[s["classification"]], s["detected_at"]))
    for s in todo:
        lvl, sid = s["classification"], s["id"]
        def decide(action, why, **extra):
            a = {"story": sid, "level": lvl, "action": action, "why": why, "mode": mode, "headline": s["headline"], **extra}
            actions.append(a)
            log(sid, "plan", f"{action}: {why}", **{k: v for k, v in a.items() if k not in ("story", "why", "headline")})
            return a
        if not cfg["BREAKING_NEWS_ENABLED"]:
            decide("NONE", "BREAKING_NEWS_ENABLED is false")
            continue
        # age of the current development (a fresh update on an older event restarts the clock)
        age_h = (now - parse_time(s.get("active_since") or s["detected_at"])).total_seconds() / 3600
        if s["verification_status"] != "VERIFIED":
            if age_h >= cfg["VERIFICATION_TIMEOUT_HOURS"]:
                s["publication_status"] = "HUMAN_REVIEW"
                decide("FLAG_HUMAN_REVIEW", f"still UNVERIFIED after {age_h:.1f}h ({s['verification']['reason']}); evidence saved")
            else:
                s["publication_status"] = "BLOCKED_UNVERIFIED"
                decide("WAIT_FOR_VERIFICATION", f"UNVERIFIED — {s['verification']['reason']}; re-check next run")
            continue
        if lvl == "IMPORTANT" and age_h >= cfg["IMPORTANT_MAX_DELAY_HOURS"]:
            s["publication_status"] = "DEMOTED_TO_DAILY"
            decide("SEND_TO_DAILY", f"held {age_h:.1f}h without a slot; covered in the next daily episode instead")
            continue
        # Rate limits
        n_level = sum(1 for p in prior if p["level"] == lvl)
        cap = cfg["MAX_BREAKING_SHORTS_PER_DAY"] if lvl == "BREAKING" else cfg["MAX_IMPORTANT_SHORTS_PER_DAY"]
        override = None
        if len(prior) >= cfg["HARD_MAX_EXTRA_SHORTS_PER_DAY"]:
            s["publication_status"] = "HELD_RATE_LIMIT" if lvl == "BREAKING" else "DEMOTED_TO_DAILY"
            decide("HOLD_RATE_LIMIT" if lvl == "BREAKING" else "SEND_TO_DAILY",
                   f"hard daily cap of {cfg['HARD_MAX_EXTRA_SHORTS_PER_DAY']} extra Shorts reached")
            continue
        if n_level >= cap:
            if lvl == "BREAKING" and s.get("emergency") and cfg["ALLOW_EMERGENCY_OVERRIDE"]:
                override = f"emergency override: {n_level}/{cap} BREAKING Shorts used today, but the story is flagged emergency ({'; '.join(s['classification_reason'][-1:])})"
            else:
                s["publication_status"] = "HELD_RATE_LIMIT" if lvl == "BREAKING" else "DEMOTED_TO_DAILY"
                decide("HOLD_RATE_LIMIT" if lvl == "BREAKING" else "SEND_TO_DAILY",
                       f"{n_level}/{cap} {lvl} Shorts already used today")
                continue
        # Timing: spacing between extras and quiet window around the daily episodes
        due = now + timedelta(minutes=cfg["PUBLISH_DELAY_MINUTES"])
        timing = []
        last = max((p["at"] for p in prior), default=None)
        gap = timedelta(minutes=cfg["MIN_MINUTES_BETWEEN_EXTRA_POSTS"])
        if last and due - last < gap:
            if lvl == "BREAKING" and cfg["BREAKING_BYPASSES_SPACING"]:
                timing.append(f"BREAKING bypasses the {cfg['MIN_MINUTES_BETWEEN_EXTRA_POSTS']}-min spacing (last extra at {iso(last)})")
            else:
                due = last + gap
                timing.append(f"spaced to {iso(due)}: {cfg['MIN_MINUTES_BETWEEN_EXTRA_POSTS']} min after the last extra post")
        moved = slot_conflict(due, cfg)
        if moved:
            if lvl == "BREAKING":
                timing.append("inside the quiet window around a daily episode, but BREAKING goes anyway")
            else:
                due = moved
                timing.append(f"moved to {iso(due)} to stay clear of the scheduled daily episode")
        # Gates
        auto = cfg["AUTO_PUBLISH_LEVEL_3"] if lvl == "BREAKING" else cfg["AUTO_PUBLISH_LEVEL_2"]
        why = [f"VERIFIED ({s['verification']['reason']})", f"{lvl} slot {n_level + 1}/{cap} today"] + ([override] if override else []) + timing
        if cfg["DRY_RUN"]:
            action = "WOULD_PUBLISH"
            s["publication_status"] = "WOULD_PUBLISH_DRY_RUN"
            gate = "DRY_RUN is on — nothing is rendered or posted; " + (
                "it would auto-publish" if auto and s["confidence"] >= cfg["MIN_CONFIDENCE_FOR_AUTO"] else "it would go to Buffer as a draft for approval")
        elif auto and s["confidence"] >= cfg["MIN_CONFIDENCE_FOR_AUTO"]:
            action = "PUBLISH"
            s["publication_status"] = "READY_TO_PUBLISH"
            gate = f"AUTO_PUBLISH_LEVEL_{RANK[lvl] + 1} is on and confidence {s['confidence']:.2f} >= {cfg['MIN_CONFIDENCE_FOR_AUTO']}"
        else:
            action = "DRAFT_FOR_APPROVAL"
            s["publication_status"] = "READY_FOR_APPROVAL"
            gate = (f"AUTO_PUBLISH_LEVEL_{RANK[lvl] + 1} is off" if not auto else
                    f"confidence {s['confidence']:.2f} below {cfg['MIN_CONFIDENCE_FOR_AUTO']}") + " — human approval needed"
        why.append(gate)
        a = decide(action, "; ".join(why), due_at=iso(due), followup=bool(s.get("needs_followup")))
        prior.append({"level": lvl, "at": due, "story": sid})
    for s in stories:
        if s["classification"] == "NORMAL" and s["publication_status"] == "PENDING":
            s["publication_status"] = "QUEUED_FOR_DAILY"
    save_stories(stories)
    return actions

# ---------------------------------------------------------------- other commands

def mark(story_id, pairs, now, run_id):
    stories = load_stories()
    s = next((x for x in stories if x["id"] == story_id), None)
    if not s:
        raise SystemExit(f"no story {story_id}")
    log = Logger(run_id, now)
    for p in pairs:
        k, _, v = p.partition("=")
        val = {"true": True, "false": False}.get(v.lower(), v)
        if k == "post":
            s["posts"].append(v)
        else:
            s[k] = val
        log(story_id, "update", f"{k} = {v}")
    if s.get("publication_status") in ("SCHEDULED", "PUBLISHED", "DRAFTED", "WOULD_PUBLISH_DRY_RUN"):
        s["needs_followup"] = False
    save_stories(stories)

def daily_candidates(now, cfg, mark_used=None):
    stories = load_stories()
    out = []
    for s in stories:
        if s["publication_status"] in ("QUEUED_FOR_DAILY", "DEMOTED_TO_DAILY") and \
           now - parse_time(s["detected_at"]) < timedelta(hours=48):
            out.append({k: s[k] for k in ("id", "headline", "summary", "classification", "assets", "source_urls",
                                          "verification_status", "market_significance", "detected_at")})
    if mark_used:
        log = Logger("daily", now)
        for s in stories:
            if s["id"] in mark_used:
                s["publication_status"] = "USED_IN_DAILY"
                log(s["id"], "publishing", "used as a story in the daily episode")
        save_stories(stories)
    # Recently covered by an extra Short — the daily show can reference it instead of repeating it.
    done = [{"id": s["id"], "headline": s["headline"]} for s in stories
               if covered(s) and now - parse_time(s["detected_at"]) < timedelta(hours=30)]
    return {"candidates": out, "already_covered_by_breaking_shorts": done}

def load_meta():
    p = os.path.join(state_dir(), "meta.json")
    return json.load(open(p)) if os.path.exists(p) else {}

def save_meta(m):
    json.dump(m, open(os.path.join(state_dir(), "meta.json"), "w"), indent=1)

def covered_in_daily(cands, cfg, now, run_id):
    """Record the stories today's scheduled daily episode covered, so no extra Short repeats them
    (a genuinely new development can still upgrade them later)."""
    meta = load_meta()
    if meta.get("daily_synced") == et_day(now):
        return {"skipped": f"daily episode for {et_day(now)} already recorded"}
    for c in cands:
        c.setdefault("proposed_level", "NORMAL")
    d = ingest(cands, cfg, now, run_id)
    ids = {x["story"] for x in d}
    stories = load_stories()
    log = Logger(run_id, now)
    for s in stories:
        if s["id"] in ids and not covered(s):
            s["publication_status"] = "USED_IN_DAILY"
            log(s["id"], "publishing", f"covered in the {et_day(now)} daily episode")
    save_stories(stories)
    meta["daily_synced"] = et_day(now)
    save_meta(meta)
    return {"recorded": sorted(ids)}

def status(cfg, now):
    stories = load_stories()
    prior = extra_posts_today(now, read_log())
    recent = [{k: s[k] for k in ("id", "headline", "classification", "assets", "verification_status",
                                 "publication_status", "detected_at", "content_created")}
              for s in stories if now - parse_time(s["detected_at"]) < timedelta(hours=cfg["DEDUPE_WINDOW_HOURS"])]
    return {
        "now": iso(now),
        "daily_episode_recorded_for": load_meta().get("daily_synced"),
        "mode": "DRY_RUN" if cfg["DRY_RUN"] else "LIVE",
        "enabled": cfg["BREAKING_NEWS_ENABLED"],
        "auto_publish": {"IMPORTANT": cfg["AUTO_PUBLISH_LEVEL_2"], "BREAKING": cfg["AUTO_PUBLISH_LEVEL_3"]},
        "extra_shorts_today": {l: sum(1 for p in prior if p["level"] == l) for l in ("IMPORTANT", "BREAKING")},
        "limits": {k: cfg[k] for k in ("MAX_IMPORTANT_SHORTS_PER_DAY", "MAX_BREAKING_SHORTS_PER_DAY",
                                        "MIN_MINUTES_BETWEEN_EXTRA_POSTS", "HARD_MAX_EXTRA_SHORTS_PER_DAY")},
        "recent_stories": recent,
    }

def report(since=None, run=None):
    stories = {s["id"]: s for s in load_stories()}
    entries = [e for e in read_log() if (not run or e["run"] == run) and (not since or parse_time(e["ts"]) >= since)]
    by = {}
    for e in entries:
        by.setdefault(e["story"], []).append(e)
    lines = []
    for sid, es in by.items():
        s = stories.get(sid, {})
        lines.append(f"\n== {s.get('headline', sid)}  [{sid}]")
        lines.append(f"   level {s.get('classification')} · {s.get('verification_status')} · status {s.get('publication_status')} · assets {', '.join(s.get('assets', []))}")
        for e in es:
            lines.append(f"   {e['ts'][11:16]}  {e['step']:<16} {e['detail']}")
            for r in (e.get("data") or {}).get("reasons", []):
                lines.append(f"{'':25}· {r}")
            for src in (e.get("data") or {}).get("sources", []):
                lines.append(f"{'':25}· {'✔' if src['counts'] else '✘'} {src['name']} ({src['category']}) — {src['note']}")
    return "\n".join(lines).strip() or "No log entries."

# ---------------------------------------------------------------- CLI

def main(argv):
    if not argv:
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    cfg = load_config()
    now = get_now(args)
    run_id = args[args.index("--run") + 1] if "--run" in args else now.astimezone(ET).strftime("run_%Y%m%d_%H%M")
    if cmd == "status":
        print(json.dumps(status(cfg, now), indent=1))
    elif cmd == "ingest":
        cands = json.load(open(args[0]))
        if isinstance(cands, dict):
            cands = cands.get("candidates", [cands])
        print(json.dumps(ingest(cands, cfg, now, run_id), indent=1, ensure_ascii=False))
    elif cmd == "covered-in-daily":
        print(json.dumps(covered_in_daily(json.load(open(args[0])), cfg, now, run_id), indent=1))
    elif cmd == "plan":
        print(json.dumps(plan(cfg, now, run_id), indent=1, ensure_ascii=False))
    elif cmd == "mark":
        mark(args[0], [a for a in args[1:] if "=" in a and not a.startswith("--")], now, run_id)
    elif cmd == "log":
        Logger(run_id, now)(args[0], args[1], args[2])
    elif cmd == "daily-candidates":
        used = args[args.index("--mark-used") + 1].split(",") if "--mark-used" in args else None
        print(json.dumps(daily_candidates(now, cfg, used), indent=1, ensure_ascii=False))
    elif cmd == "report":
        since = parse_time(args[args.index("--since") + 1]) if "--since" in args else None
        print(report(since, args[args.index("--run") + 1] if "--run" in args else None))
    else:
        print(__doc__)
        sys.exit(1)

if __name__ == "__main__":
    main(sys.argv[1:])
