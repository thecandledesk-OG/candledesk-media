#!/usr/bin/env python3
"""Tests for the Breaking News Engine. Run: python3 breaking/tests/test_engine.py
Uses a throwaway state folder and fixed clock times; never touches real state or posts anything."""
import json, os, sys, tempfile, copy
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import engine as E
import script_tools as T

BASE_CFG = E.load_config()
FIX = json.load(open(os.path.join(HERE, "fixtures.json")))
RESULTS = []

def fresh(**over):
    d = tempfile.mkdtemp(prefix="bne-")
    os.environ["BREAKING_STATE_DIR"] = d
    cfg = copy.deepcopy(BASE_CFG)
    # Tests start from the safe defaults whatever the live config says.
    cfg.update({"BREAKING_NEWS_ENABLED": True, "DRY_RUN": True, "AUTO_PUBLISH_LEVEL_2": False, "AUTO_PUBLISH_LEVEL_3": False})
    cfg.update(over)
    return cfg

def t(name):
    def wrap(fn):
        try:
            fn()
            RESULTS.append((name, True, ""))
        except AssertionError as e:
            RESULTS.append((name, False, str(e)))
        except Exception as e:  # noqa
            RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
        return fn
    return wrap

def at(s):
    return E.parse_time(s)

def cand(key, **over):
    c = copy.deepcopy(FIX[key])
    c.update(over)
    return c

# ---------------------------------------------------------------- verification

@t("social posts alone never verify a story")
def _():
    cfg = fresh()
    v = E.verify(FIX["sol_rumor_social_only"]["sources"], cfg)
    assert v["status"] == "UNVERIFIED", v
    assert all(not s["counts"] for s in v["sources_checked"])

@t("two outlets carrying the same wire story count once")
def _():
    cfg = fresh()
    v = E.verify(FIX["eth_syndicated"]["sources"], cfg)
    assert v["independent_sources"] == 1 and v["status"] == "UNVERIFIED", v

@t("primary + independent outlet verifies")
def _():
    cfg = fresh()
    v = E.verify(FIX["btc_etf_approval"]["sources"], cfg)
    assert v["status"] == "VERIFIED" and v["has_primary"], v

@t("unopened or non-confirming sources don't count")
def _():
    cfg = fresh()
    srcs = copy.deepcopy(FIX["btc_etf_approval"]["sources"])
    srcs[0]["checked"] = False
    srcs[1]["confirms"] = False
    assert E.verify(srcs, cfg)["status"] == "UNVERIFIED"

@t("two unknown 'primary' domains are not enough without a known outlet")
def _():
    cfg = fresh()
    srcs = [{"url": "https://someproject.io/blog/x", "name": "A", "kind": "primary", "checked": True, "confirms": True},
            {"url": "https://otherproject.xyz/news", "name": "B", "kind": "primary", "checked": True, "confirms": True}]
    assert E.verify(srcs, cfg)["status"] == "UNVERIFIED"

# ---------------------------------------------------------------- classification

@t("price rule: BTC -13% is BREAKING, -4% is NORMAL")
def _():
    cfg = fresh()
    now = at("2026-10-05T14:00")
    big = cand("btc_crash", metrics={"pct_move_24h": -13})
    small = cand("btc_crash", metrics={"pct_move_24h": -4})
    assert E.rule_level(big, cfg, now)[0] == "BREAKING"
    assert E.rule_level(small, cfg, now)[0] == "NORMAL"

@t("hack rule uses dollar thresholds")
def _():
    cfg = fresh()
    now = at("2026-10-05T14:00")
    assert E.rule_level(cand("eth_defi_hack"), cfg, now)[0] == "BREAKING"
    assert E.rule_level(cand("eth_defi_hack", metrics={"loss_usd": 25e6}), cfg, now)[0] == "IMPORTANT"

@t("expected future event stays NORMAL until it happens (XRPL-style upgrade)")
def _():
    cfg = fresh()
    lvl, notes = E.rule_level(cand("xrpl_upgrade_expected"), cfg, at("2026-10-04T09:00"))
    assert lvl == "NORMAL", notes

@t("old news is not breaking news")
def _():
    cfg = fresh()
    lvl, _ = E.rule_level(cand("btc_etf_approval"), cfg, at("2026-10-07T09:00"))
    assert lvl == "NORMAL"

# ---------------------------------------------------------------- ingest / duplicates

@t("NORMAL story is queued for the daily episode, no extra video")
def _():
    cfg = fresh()
    now = at("2026-10-05T10:00")
    d = E.ingest([cand("sol_minor_update")], cfg, now, "t")
    assert d[0]["level"] == "NORMAL"
    acts = E.plan(cfg, now, "t")
    assert acts == []
    dc = E.daily_candidates(now, cfg)
    assert len(dc["candidates"]) == 1

@t("different headlines for the same event are grouped into one story")
def _():
    cfg = fresh()
    now = at("2026-10-05T14:00")
    E.ingest([cand("btc_etf_approval")], cfg, now, "t")
    d = E.ingest([cand("btc_etf_approval_other_headline")], cfg, now + timedelta(minutes=30), "t")
    assert d[0]["result"] in ("MERGED", "NOW_VERIFIED"), d
    assert len(E.load_stories()) == 1

@t("generic terms like 'layer-2' don't merge different stories")
def _():
    cfg = fresh()
    now = at("2026-10-05T14:00")
    a = cand("eth_defi_hack", headline="Layer-2 network Base adds new fee model", summary="Base changes layer-2 fees.", event_type="other", metrics={})
    b = cand("eth_syndicated", headline="Another layer-2 shuts down after deposits fall", summary="A small layer-2 is closing.", event_type="protocol_shutdown")
    E.ingest([a, b], cfg, now, "t")
    assert len(E.load_stories()) == 2

@t("unrelated stories about different coins are not merged")
def _():
    cfg = fresh()
    now = at("2026-10-05T14:00")
    E.ingest([cand("btc_etf_approval"), cand("eth_defi_hack")], cfg, now, "t")
    assert len(E.load_stories()) == 2

@t("XRPL: expected upgrade -> NORMAL; activation reported later -> upgraded to IMPORTANT, one event")
def _():
    cfg = fresh()
    E.ingest([cand("xrpl_upgrade_expected")], cfg, at("2026-10-04T09:00"), "t")
    d = E.ingest([cand("xrpl_upgrade_activated")], cfg, at("2026-10-08T15:00"), "t")
    assert d[0]["result"] == "UPGRADED", d
    s = E.load_stories()
    assert len(s) == 1 and s[0]["classification"] == "IMPORTANT"
    acts = E.plan(cfg, at("2026-10-08T15:00"), "t")
    assert acts[0]["action"] == "WOULD_PUBLISH", acts

@t("a new development is verified on its own sources, not the older reports")
def _():
    cfg = fresh()
    E.ingest([cand("xrpl_upgrade_expected")], cfg, at("2026-10-04T09:00"), "t")
    weak = cand("xrpl_upgrade_activated", sources=[FIX["xrpl_upgrade_activated"]["sources"][0]])
    E.ingest([weak], cfg, at("2026-10-08T15:00"), "t")
    s = E.load_stories()[0]
    assert s["verification_status"] == "UNVERIFIED", s["verification"]
    acts = E.plan(cfg, at("2026-10-08T15:00"), "t")
    assert acts[0]["action"] == "WAIT_FOR_VERIFICATION"

@t("unverified story waits, then gets flagged for human review with evidence saved")
def _():
    cfg = fresh()
    now = at("2026-10-05T10:00")
    E.ingest([cand("sol_rumor_social_only")], cfg, now, "t")
    a1 = E.plan(cfg, now, "t")
    assert a1[0]["action"] == "WAIT_FOR_VERIFICATION"
    a2 = E.plan(cfg, now + timedelta(hours=7), "t")
    assert a2[0]["action"] == "FLAG_HUMAN_REVIEW"
    s = E.load_stories()[0]
    assert s["publication_status"] == "HUMAN_REVIEW" and s["source_urls"]

@t("later corroboration releases an unverified story")
def _():
    cfg = fresh()
    now = at("2026-10-05T10:00")
    E.ingest([cand("sol_outage_one_source")], cfg, now, "t")
    assert E.plan(cfg, now, "t")[0]["action"] == "WAIT_FOR_VERIFICATION"
    d = E.ingest([cand("sol_outage_second_source")], cfg, now + timedelta(minutes=60), "t")
    assert d[0]["result"] == "NOW_VERIFIED", d
    assert E.plan(cfg, now + timedelta(minutes=60), "t")[0]["action"] == "WOULD_PUBLISH"

# ---------------------------------------------------------------- planning, limits, gates

def many_important(n, start):
    out = []
    for i in range(n):
        c = cand("generic_important")
        c["headline"] = f"Exchange {['Alpha','Bravo','Charlie','Delta','Echo'][i]} lists spot trading for institutional desks {i}"
        c["summary"] = f"Unique summary number {i} about venue {['Alpha','Bravo','Charlie','Delta','Echo'][i]}"
        c["assets"] = [["BTC", "ETH", "SOL", "XRP", "BNB"][i]]
        for s in c["sources"]:
            s["url"] += f"?n={i}"
        for s in c["sources"]:
            s["published"] = E.iso(start)
        out.append(c)
    return out

@t("priority: BREAKING is planned before IMPORTANT")
def _():
    cfg = fresh()
    now = at("2026-10-05T14:00")
    E.ingest(many_important(1, now) + [cand("btc_etf_approval")], cfg, now, "t")
    acts = E.plan(cfg, now, "t")
    assert [a["level"] for a in acts] == ["BREAKING", "IMPORTANT"], acts

@t("IMPORTANT limit: 4th IMPORTANT of the day goes to the daily show")
def _():
    cfg = fresh(MIN_MINUTES_BETWEEN_EXTRA_POSTS=0)
    now = at("2026-10-05T11:00")
    E.ingest(many_important(4, now), cfg, now, "t")
    acts = E.plan(cfg, now, "t")
    assert [a["action"] for a in acts].count("WOULD_PUBLISH") == 3, acts
    assert acts[3]["action"] == "SEND_TO_DAILY"

@t("spacing: second IMPORTANT is pushed 60 min after the first")
def _():
    cfg = fresh()
    now = at("2026-10-05T11:00")
    E.ingest(many_important(2, now), cfg, now, "t")
    acts = E.plan(cfg, now, "t")
    d0, d1 = at(acts[0]["due_at"]), at(acts[1]["due_at"])
    assert d1 - d0 >= timedelta(minutes=60), acts

@t("quiet window: IMPORTANT due at 8:05 AM moves past the 8:15 episode; BREAKING doesn't")
def _():
    cfg = fresh()
    now = at("2026-10-05T07:50")
    E.ingest(many_important(1, now), cfg, now, "t")
    a = E.plan(cfg, now, "t")[0]
    assert at(a["due_at"]) >= at("2026-10-05T08:45"), a
    cfg = fresh()
    c = cand("btc_etf_approval")
    for s in c["sources"]:
        s["published"] = "2026-10-05T07:40"
    E.ingest([c], cfg, now, "t")
    b = E.plan(cfg, now, "t")[0]
    assert at(b["due_at"]) == at("2026-10-05T08:05"), b

@t("BREAKING over its cap needs the emergency flag (override is logged)")
def _():
    cfg = fresh(MAX_BREAKING_SHORTS_PER_DAY=1)
    now = at("2026-10-05T14:00")
    c1 = cand("btc_etf_approval")
    c2 = cand("eth_defi_hack", emergency=False)
    c3 = cand("sol_outage_verified", emergency=True)
    for c in (c1, c2, c3):
        for s in c["sources"]:
            s["published"] = "2026-10-05T13:30"
    E.ingest([c1, c2, c3], cfg, now, "t")
    acts = {a["headline"][:12]: a for a in E.plan(cfg, now, "t")}
    kinds = sorted(a["action"] for a in acts.values())
    assert kinds == ["HOLD_RATE_LIMIT", "WOULD_PUBLISH", "WOULD_PUBLISH"], acts
    assert any("emergency override" in a["why"] for a in acts.values())

@t("gates: DRY_RUN never publishes; LIVE with auto off makes a draft; LIVE with auto on publishes")
def _():
    now = at("2026-10-05T14:00")
    c = cand("btc_etf_approval")
    for s in c["sources"]:
        s["published"] = "2026-10-05T13:30"
    for over, want in (({}, "WOULD_PUBLISH"),
                       ({"DRY_RUN": False}, "DRAFT_FOR_APPROVAL"),
                       ({"DRY_RUN": False, "AUTO_PUBLISH_LEVEL_3": True}, "PUBLISH")):
        cfg = fresh(**over)
        E.ingest([copy.deepcopy(c)], cfg, now, "t")
        a = E.plan(cfg, now, "t")[0]
        assert a["action"] == want, (over, a)

@t("low confidence blocks auto-publish even when it's switched on")
def _():
    cfg = fresh(DRY_RUN=False, AUTO_PUBLISH_LEVEL_3=True)
    now = at("2026-10-05T14:00")
    c = cand("btc_etf_approval", confidence=0.5)
    for s in c["sources"]:
        s["published"] = "2026-10-05T13:30"
    E.ingest([c], cfg, now, "t")
    assert E.plan(cfg, now, "t")[0]["action"] == "DRAFT_FOR_APPROVAL"

@t("master switch off: nothing planned")
def _():
    cfg = fresh(BREAKING_NEWS_ENABLED=False)
    now = at("2026-10-05T14:00")
    c = cand("btc_etf_approval")
    for s in c["sources"]:
        s["published"] = "2026-10-05T13:30"
    E.ingest([c], cfg, now, "t")
    assert E.plan(cfg, now, "t")[0]["action"] == "NONE"

@t("same story is never planned twice")
def _():
    cfg = fresh()
    now = at("2026-10-05T14:00")
    c = cand("btc_etf_approval")
    for s in c["sources"]:
        s["published"] = "2026-10-05T13:30"
    E.ingest([c], cfg, now, "t")
    E.plan(cfg, now, "t")
    E.ingest([cand("btc_etf_approval_other_headline")], cfg, now + timedelta(minutes=60), "t")
    assert E.plan(cfg, now + timedelta(minutes=60), "t") == []

@t("a story the daily episode already covered doesn't get an extra Short; once per day")
def _():
    cfg = fresh()
    now = at("2026-10-05T08:30")
    daily = cand("btc_etf_approval", proposed_level="NORMAL")
    for s in daily["sources"]:
        s["published"] = "2026-10-05T07:00"
    r = E.covered_in_daily([daily], cfg, now, "t")
    assert len(r["recorded"]) == 1
    assert "skipped" in E.covered_in_daily([daily], cfg, now, "t")
    later = cand("btc_etf_approval_other_headline")
    for s in later["sources"]:
        s["published"] = "2026-10-05T09:00"
    E.ingest([later], cfg, now + timedelta(hours=1), "t")
    assert E.plan(cfg, now + timedelta(hours=1), "t") == []

@t("audit log records every step")
def _():
    cfg = fresh()
    now = at("2026-10-05T14:00")
    c = cand("btc_etf_approval")
    for s in c["sources"]:
        s["published"] = "2026-10-05T13:30"
    E.ingest([c], cfg, now, "t")
    E.plan(cfg, now, "t")
    steps = {e["step"] for e in E.read_log()}
    assert {"discovered", "sources_checked", "verification", "classification", "duplicate_check", "plan"} <= steps, steps
    assert "VERIFIED" in E.report()

# ---------------------------------------------------------------- scripts

@t("script lint passes a clean script and catches hype")
def _():
    good = FIX["script_good"]
    r = T.lint(good, BASE_CFG)
    assert r["ok"], r
    bad = copy.deepcopy(good)
    bad["scenes"][2]["vo"] = "This coin is about to explode, buy before it's too late."
    r = T.lint(bad, BASE_CFG)
    assert not r["ok"] and any("banned" in e for e in r["errors"]), r
    bad2 = copy.deepcopy(good)
    bad2["scenes"][3]["vo"] = "Bitcoin will rally after this, no doubt about that at all."
    assert any("prediction" in e for e in T.lint(bad2, BASE_CFG)["errors"])

@t("breaking player keeps the news player intact except the marked parts")
def _():
    src = open(os.path.join(os.path.dirname(os.path.dirname(HERE)), "breaking", "tests", "player_sample.html")).read()
    out = T.build_player(src, FIX["script_good"])
    assert "BREAKING · OCT 8" in out and "SOURCES:" in out
    assert out.count("const scenes=[") == 1 and "who:'all'" in out
    assert src.split("<script>")[0].count("<div") == out.split("<script>")[0].count("<div")

if __name__ == "__main__":
    ok = sum(1 for _, p, _ in RESULTS if p)
    for name, p, msg in RESULTS:
        print(("PASS " if p else "FAIL ") + name + ("" if p else f"\n     {msg}"))
    print(f"\n{ok}/{len(RESULTS)} passed")
    sys.exit(0 if ok == len(RESULTS) else 1)
