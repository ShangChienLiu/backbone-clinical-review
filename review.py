"""Queries over the saved abstraction. Every numerical result is computed here."""
from __future__ import annotations

import itertools
import json
import re
from collections import Counter
from datetime import date, timedelta

from certainty import ALGORITHM_VERSION, determine
from extract import MONTHS, days
from ledger import encode, get_claims, get_events, patients
from reconcile import assessments, duration

DAY = timedelta(days=1)
WEEK = timedelta(days=7)


class UnsupportedQuestion(ValueError):
    """A request outside the available review types."""


def week_start(day):
    return day - DAY * day.weekday()


def dose(events):
    confirmed = [e for e in events if e["attendance"] == "attended"]
    unresolved = [e for e in events if e["attendance"] == "unresolved"]
    eligible = confirmed + unresolved
    dates = {e["date"] for e in confirmed}
    possible_dates = dates | {d for e in unresolved for d in e["date_candidates"]}
    lo = sum(e["minute_min"] for e in eligible)
    hi = None if any(e["minute_max"] is None for e in eligible) else sum(e["minute_max"] for e in eligible)
    lower = lo
    upper = hi
    has_intervals = all(e["interval_options"] and e["date"] for e in eligible)
    scenarios = [0]
    for day in sorted(possible_dates):
        options = [e["interval_options"] for e in eligible if e["date"] == day]
        combinations = 1
        for option in options:
            combinations *= len(option)
        if not has_intervals or combinations > 4096:
            scenarios = []
            break
        same_day = sorted({duration([span for part in parts for span in part])
                           for parts in itertools.product(*options)})
        scenarios = sorted({v + p for v in scenarios for p in same_day})
    if scenarios and has_intervals:
        lower, upper = min(scenarios), max(scenarios) if hi is not None else None
    else:
        scenarios = []
        # Overlapping possible contacts cannot justify the sum of independent lower bounds.
        lower = sum(max((e["minute_min"] for e in confirmed if e["date"] == day), default=0)
                    for day in dates)
    categories = {}
    for kind in sorted({e["service"] for e in eligible}):
        group = [e for e in eligible if e["service"] == kind]
        certain = Counter(e["attendance"] for e in group)
        categories[kind] = {"confirmed": certain["attended"], "unresolved": certain["unresolved"],
                            "minute_min": sum(e["minute_min"] for e in group),
                            "minute_max": None if any(e["minute_max"] is None for e in group) else
                            sum(e["minute_max"] for e in group)}
    return {
        "sessions_confirmed": len(confirmed), "sessions_possible_additional": len(unresolved),
        "days_confirmed": len(dates), "days_possible_upper": len(possible_dates) if all(e["date_candidates"] for e in unresolved) else None,
        "minute_min": lower, "minute_max": upper, "minute_values": scenarios,
        "hour_min": lower / 60, "hour_max": upper / 60 if upper is not None else None,
        "by_service": categories,
    }


def coverage(claims, start, end):
    for c in claims:
        if c["kind"] == "coverage" and c["value"][0] <= start <= end <= c["value"][1]:
            return c
    return None


def episode_covering(facts, start, end):
    return next((c for c in facts if c["kind"] == "episode" and c["value"][0] <= start <= end <= c["value"][1]), None)


def summarize_week(events, facts, day, first, last):
    monday, sunday = day.isoformat(), (day + 6 * DAY).isoformat()
    plans = [p for p in facts if p["kind"] == "plan" and p["date"]]
    effective = max((p["date"] for p in plans if p["date"] <= monday), default=None)
    latest = [p for p in plans if p["date"] == effective]
    midweek = [p for p in plans if monday < p["date"] <= sunday]
    plan = latest[-1] if latest and all(p["value"] == latest[-1]["value"] for p in latest) and not midweek else None
    episode = episode_covering(facts, first, last)
    covered = coverage(facts, first, last)
    starts_at_boundary = first == monday or (episode and first == episode["value"][0])
    ends_at_boundary = last == sunday or (episode and last == episode["value"][1])
    complete = bool(covered and starts_at_boundary and ends_at_boundary)
    total = dose(events)
    goal_status, proof = determine(events, total, plan, covered, complete, first, last)
    return {
        "monday": monday, "sunday": sunday, "review_start": first, "review_end": last,
        "plan": plan, "midweek_plan_changes": midweek, "coverage": covered, "episode_scope": episode,
        "complete_week_claimed": complete, "goal_status": goal_status, "certainty": proof,
        "limitations": (["A plan effective for the full week could not be established."] if not plan else [])
                       + (["Complete attendance coverage for the review dates could not be established."] if not covered else [])
                       + (["The requested review window covers part of this calendar week."] if first != monday or last != sunday else []),
        "events": [e["event"] for e in events], **total,
    }


def weeks_between(first, last):
    day = week_start(date.fromisoformat(first))
    stop = date.fromisoformat(last)
    while day <= stop:
        yield day
        day += WEEK


def refresh_weeks(db, patient_id, changed_dates, policy_changes):
    if not changed_dates and not policy_changes:
        return 0
    facts = get_claims(db, patient_id, ("plan", "coverage", "episode"))
    dirty = {week_start(date.fromisoformat(day)) for day in changed_dates if day}
    # Missing dates can affect every previously known week. Policies can extend
    # the range or change how future weeks are evaluated without adding a visit.
    if None in changed_dates or policy_changes:
        known = {date.fromisoformat(r[0]) for r in db.execute(
            "SELECT monday FROM patient_weeks WHERE patient_id=?", (patient_id,))}
        for row in db.execute("SELECT date,payload FROM events WHERE patient_id=?", (patient_id,)):
            for day in [row["date"]] if row["date"] else json.loads(row["payload"])["date_candidates"]:
                known.add(week_start(date.fromisoformat(day)))
        for claim in facts:
            if claim["kind"] in {"coverage", "episode"}:
                known.update(weeks_between(*claim["value"]))
        if None in changed_dates:
            dirty.update(known)
        for claim in policy_changes:
            if claim["kind"] in {"coverage", "episode"}:
                dirty.update(weeks_between(*claim["value"]))
            elif claim["date"]:
                begins = week_start(date.fromisoformat(claim["date"]))
                later = [p["date"] for p in facts if p["kind"] == "plan" and p["date"] > claim["date"]]
                next_plan = week_start(date.fromisoformat(min(later))) if later else None
                dirty.update(d for d in known if d >= begins and (next_plan is None or d <= next_plan))
    episodes = [c["value"] for c in facts if c["kind"] == "episode"]
    for day in sorted(dirty):
        monday, sunday = day.isoformat(), (day + 6 * DAY).isoformat()
        overlaps = [(max(monday, start), min(sunday, end)) for start, end in episodes
                    if start <= sunday and end >= monday]
        first, last = overlaps[0] if len(overlaps) == 1 else (monday, sunday)
        events = get_events(db, patient_id, first, last)
        item = summarize_week(events, facts, day, first, last)
        db.execute("""INSERT INTO patient_weeks VALUES(?,?,?)
                      ON CONFLICT(patient_id,monday) DO UPDATE SET payload=excluded.payload""",
                   (patient_id, monday, encode(item)))
    return len(dirty)


def cached_weeks(db, patient_id, start, end):
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='patient_weeks'").fetchone()
    if not exists:
        return {}
    first = week_start(date.fromisoformat(start)).isoformat()
    return {monday: json.loads(payload) for monday, payload in db.execute(
        "SELECT monday,payload FROM patient_weeks WHERE patient_id=? AND monday BETWEEN ? AND ?",
        (patient_id, first, end))}


def review_weeks(db, patient_id, start, end, events=None, facts=None):
    cached = cached_weeks(db, patient_id, start, end)
    weeks = []
    for day in weeks_between(start, end):
        first, last = max(day.isoformat(), start), min((day + 6 * DAY).isoformat(), end)
        existing = cached.get(day.isoformat())
        if (existing and (existing["review_start"], existing["review_end"]) == (first, last)
                and existing.get("certainty", {}).get("algorithm_version") == ALGORITHM_VERSION):
            weeks.append(existing)
            continue
        if facts is None:
            facts = get_claims(db, patient_id, ("plan", "coverage", "episode"))
        if events is None:
            subset = get_events(db, patient_id, first, last)
        else:
            subset = [e for e in events if first <= (e["date"] or "") <= last or
                      (not e["date"] and (not e["date_candidates"] or any(first <= d <= last for d in e["date_candidates"])))]
        weeks.append(summarize_week(subset, facts, day, first, last))
    return weeks


def window_review(db, patient_id, start, end):
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("Review start is later than review end")
    events = get_events(db, patient_id, start, end)
    facts = get_claims(db, patient_id, ("plan", "coverage", "episode"))
    episode = episode_covering(facts, start, end)
    weeks = review_weeks(db, patient_id, start, end, events, facts)
    window_coverage = coverage(facts, start, end)
    return {
        "patient_id": patient_id, "window": [start, end], "coverage": window_coverage, "episode_scope": episode,
        "scope": "supplied records with register coverage" if window_coverage else
                 "documented evidence only; complete coverage is not established",
        "total": dose(events), "weeks": weeks, "events": events,
    }


def symptom_review(db, patient_id, start, end, focus=()):
    claims = get_claims(db, patient_id, ("assessment", "observation", "retrospective_summary"))
    rows = [c for c in claims if c["date"] and start <= c["date"] <= end]
    checks = assessments(rows)
    flags = []
    for a in checks:
        if a["score"] is not None and set(a["retrospective_scores"]) - {a["score"]}:
            flags.append(f'{a["date"]} {a["instrument"]}: a later reference disagrees with the direct assessment.')
    observations = sorted((c for c in rows if c["kind"] == "observation"),
                          key=lambda c: (c["date"], c["path"], c["line"]))
    return {"patient_id": patient_id, "window": [start, end], "assessments": checks,
            "observations": observations, "focus_observations": [c for c in observations if c["date"] in focus],
            "retrospective_summaries": [c for c in rows if c["kind"] == "retrospective_summary"], "issues": flags}


def cohort_review(db, start, end, *, compact=False):
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("Review start is later than review end")
    definite, uncertain, comparison = [], [], []
    for patient_id, names in patients(db).items():
        bounds = get_claims(db, patient_id, ("episode",))
        if len(bounds) == 1:
            first, last = bounds[0]["value"]
            first, last = max(first, start), min(last, end)
            if first > last:
                continue
        else:
            first, last = start, end
        review = None if compact else window_review(db, patient_id, first, last)
        weeks = review_weeks(db, patient_id, first, last) if compact else review["weeks"]
        pair_results = []
        for a, b in zip(weeks, weeks[1:]):
            if not a["plan"] or not b["plan"]:
                continue
            outcomes = {a["goal_status"], b["goal_status"]}
            if "met" in outcomes:
                continue
            if not all(w["complete_week_claimed"] or w["events"] or w["episode_scope"] for w in (a, b)):
                continue
            pair_results.append({"monday_first": a["monday"], "monday_second": b["monday"],
                                 "statuses": [a["goal_status"], b["goal_status"]],
                                 "events_first": a["events"], "events_second": b["events"],
                                 "coverage_first": a["complete_week_claimed"], "coverage_second": b["complete_week_claimed"]})
        if pair_results:
            item = {"patient_id": patient_id, "names": names, "pairs": pair_results}
            (definite if any(set(p["statuses"]) == {"not met"} for p in pair_results) else uncertain).append(item)
        if any(w["events"] for w in weeks):
            summary = {"patient_id": patient_id, "names": names, "window": [first, last]}
            if review is not None:
                summary.update(weeks=weeks, total=review["total"])
            comparison.append(summary)
    result = {"window": [start, end], "two_consecutive_below": definite,
              "eligibility_depends_on_unresolved_documentation": uncertain, "patients": comparison}
    if compact:
        result["detail"] = "summary"
    return result


def plan_changes(db, patient_id, start, end):
    claims = get_claims(db, patient_id, ("plan", "coverage"))
    plans = sorted([p for p in claims if p["kind"] == "plan" and p["date"] <= end], key=lambda p: p["date"])
    results = []
    for old, new in zip(plans, plans[1:]):
        change = new["date"]
        if change <= start:
            continue
        before_end = (date.fromisoformat(change) - DAY).isoformat()
        before_start = max(old["date"], start)
        before = window_review(db, patient_id, before_start, before_end)
        next_plans = [p["date"] for p in plans if p["date"] > change]
        after_end = min(end, (date.fromisoformat(next_plans[0]) - DAY).isoformat()) if next_plans else end
        after = window_review(db, patient_id, change, after_end)
        results.append({"effective_date": change, "previous_plan": old, "new_plan": new,
                        "before": {"window": before["window"], "total": before["total"], "weeks": before["weeks"]},
                        "after": {"window": after["window"], "total": after["total"], "weeks": after["weeks"]}})
    return results


def explicit_dates(text, year=None):
    # Expand same-month ranges such as March 2–29, 2026 without mistaking the
    # trailing year for the year of a different service date.
    range_pattern = r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})\s*[-–—]\s*(\d{1,2}),?\s*(20\d{2})\b"
    text = re.sub(range_pattern, lambda m: f"{m[1]} {m[2]}, {m[4]} to {m[1]} {m[3]}, {m[4]}", text, flags=re.I)
    if year is None:
        match = re.search(r"\b20\d{2}\b", text)
        year = match[0] if match else None
    return days(text, year)


def question_type(text):
    if re.search(
        r"\b(?:which|what)\s+(?:specific\s+)?(?:medications?|drugs?|prescriptions?|diagnos[ie]s|dosages?|billing codes?)\b"
        r"|\bhow (?:many|much)\s+(?:doses?|medications?|visits were billed|did .{0,45} cost)\b"
        r"|\b(?:who treated|which (?:clinician|therapist|provider)|what caused)\b", text
    ):
        match = None
    elif re.search(r"\b(?:which|all) patients\b", text):
        match = ("cohort" if re.search(
            r"(?<![\w-])(?:two|2)\s+consecutive weeks\b|\bhad consecutive weeks\b", text
        ) and re.search(r"\b(?:below|fell short|under|did not meet)\b", text) and
                 re.search(r"\b(?:plan|goal|requirements?)\b", text) else None)
    elif re.search(r"\b(?:before and after|plan changes?)\b", text) and re.search(r"\b(?:plan|therapy)\b", text):
        match = "plan_changes"
    elif re.search(r"\breconstruct (?:the )?(?:care|visits?|sessions?|encounters?)\b"
                   r"|\b(?:what happened on|day.by.day)\b", text):
        match = "details"
    elif re.search(
        r"\b(?:summarize|describe|review|show|list)\b[^?.]{0,75}\b(?:symptoms?|symptom course|assessments?)\b"
        r"|\bsymptoms?\s+(?:progress|change|improve|worsen|review)\b"
        r"|\b(?:latest |first |last )?(?:phq.?9|gad.?7)(?: scores?|\?|$)"
        r"|\bwhich symptom assessments?\b|\bwhy\b[^?.]{0,90}\badditional individual contact\b", text
    ):
        match = "symptoms"
    elif re.search(
        r"\bhow (?:many|much) (?:therapy|psychotherapy|sessions?|treatment minutes|(?:distinct )?therapy days)\b"
        r"|\bhow long\b.{0,60}\b(?:therapy|session|encounter)\b"
        r"|\b(?:met|meet|below|reach)\b.{0,60}\b(?:goal|plan requirements?)\b"
        r"|\b(?:weekly goal met|total therapy minutes|visits and minutes)\b"
        r"|\b(?:therapy|attendance|treatment) (?:summary|review|totals?)\b", text
    ):
        match = "therapy"
    else:
        match = None
    if match is None:
        raise UnsupportedQuestion("Unsupported question. Choose therapy sessions, time, or goals, day reconstruction, "
                                  "symptoms or assessments, plan changes, or patients with two consecutive weeks below goal.")
    return match


def route(db, text, *, patient_id=None, start=None, end=None, context=None):
    context = context or {}
    kind = question_type(text.casefold())
    is_cohort = kind == "cohort"
    match = set()
    excluded = set()
    if not is_cohort:
        available = patients(db)
        for key, names in available.items():
            for token in [key, *names, *(n.split()[0] for n in names if n)]:
                for mention in re.finditer(r"\b" + re.escape(token) + r"\b", text, re.I):
                    (excluded if re.search(r"\bnot\s+$", text[:mention.start()], re.I) else match).add(key)
        if len(match) > 1:
            raise ValueError("Question mentions multiple patients. Use collection mode or a single patient.")
        if match:
            named = match.pop()
            if patient_id and patient_id != named:
                raise ValueError("The named patient conflicts with --patient.")
            patient_id = named
        if patient_id in excluded:
            raise ValueError("The requested patient is explicitly excluded.")
        if not patient_id and context.get("patient_id") not in excluded:
            patient_id = context.get("patient_id")
        if not patient_id and len(available) == 1 and not excluded:
            patient_id = next(iter(available))
    if is_cohort or context.get("patient_id") != patient_id:
        context = {}
    reference = start or context.get("start")
    week_hints = re.findall(r"\bweek of\s+(20\d{2}-\d{2}-\d{2})\b", text, re.I)
    if len(week_hints) > 1:
        raise ValueError("Supply one date window for the review.")
    if week_hints and kind != "details":
        monday = week_start(date.fromisoformat(week_hints[0]))
        dates = [monday.isoformat(), (monday + 6 * DAY).isoformat()]
    else:
        dates = explicit_dates(text, reference[:4] if reference else None)
    focus = tuple(dict.fromkeys(dates))
    if kind == "symptoms" and len(focus) == 1 and "during the episode" in text.casefold():
        start = start or context.get("start") or (min(dates) if dates else None)
        end = end or context.get("end") or (max(dates) if dates else None)
    else:
        start = start or (min(dates) if dates else context.get("start"))
        end = end or (max(dates) if dates else context.get("end"))
    if not start or not end:
        raise ValueError("A review start and end are required. Pass --start and --end or state the range in the question.")
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("Review start is later than review end")
    if is_cohort:
        result = cohort_review(db, start, end)
    elif not patient_id:
        raise ValueError("Identify the patient in the question or supply --patient.")
    elif kind == "symptoms":
        result = symptom_review(db, patient_id, start, end, focus)
    elif kind == "plan_changes":
        result = {"patient_id": patient_id, "changes": plan_changes(db, patient_id, start, end)}
    elif kind == "details":
        focus = focus or (start,)
        if any(not start <= day <= end for day in focus):
            raise ValueError("A requested day is outside the review dates.")
        results = [window_review(db, patient_id, one, one) for one in focus]
        result = {"patient_id": patient_id, "days": results}
    else:
        result = window_review(db, patient_id, start, end)
    # Reconstructing selected days in a batch leaves the broader episode scope available.
    return result, {"patient_id": patient_id, "start": context.get("start", start) if kind == "details" else start,
                    "end": context.get("end", end) if kind == "details" else end}
