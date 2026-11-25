"""Resolve field-level assertions and preserve documented alternatives."""
from __future__ import annotations

import itertools
import json
from collections import defaultdict

from extract import THERAPY


def unique(items):
    return [json.loads(value) for value in sorted({json.dumps(x, sort_keys=True) for x in items})]


def merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if start >= end:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return merged


def subtract(active, inactive):
    result = merge_intervals(active)
    for start, end in merge_intervals(inactive):
        pieces = []
        for low, high in result:
            if start >= high or end <= low:
                pieces.append([low, high])
            else:
                pieces.extend([[low, min(start, high)], [max(end, low), high]])
        result = merge_intervals(pieces)
    return result


def duration(spans):
    return sum(high - low for low, high in merge_intervals(spans))


def choose(claims, kind, order):
    for roles in order:
        found = [c for c in claims if c["kind"] == kind and c["role"] in roles]
        if kind == "attendance":
            found = [c for c in found if c["value"] != "unknown"]
        if found:
            return unique(c["value"] for c in found), found
    return [], []


def reconcile_event(event_id, claims, global_group_rules):
    claims = sorted(claims, key=lambda c: (c["path"], c["line"], c["kind"]))
    issues = []
    used = defaultdict(list)
    dated = unique(c["date"] for c in claims if c["date"] and c["role"] not in {"copy", "administrative"})
    types = unique(c["service"] for c in claims if c["service"] != "unknown" and c["role"] not in {"copy", "administrative"})
    service = types[0] if len(types) == 1 else "unknown"
    event = {"event": event_id, "date": dated[0] if len(dated) == 1 else None,
             "date_candidates": dated, "service": service, "attendance": "unresolved",
             "minute_min": 0, "minute_max": None, "minute_values": [],
             "interval_options": [], "claims": claims, "issues": issues, "decision_sources": used}
    if len(dated) != 1:
        issues.append("Service date missing or conflicting")
    if service == "unknown":
        issues.append("Service category missing or conflicting")
    if any(c["role"] == "copy" for c in claims):
        issues.append("Retransmitted material linked to original encounter")
    if any(c["role"] == "administrative" for c in claims):
        issues.append("Billing and template assertions excluded from attendance and minute decisions")
    if any(c["role"] == "correction" for c in claims):
        issues.append("Explicit correction supersedes only the specified evidence fields")
    if any(c["kind"] == "platform" for c in claims):
        issues.append("Connection segments linked to a single appointment")
    if any(c["kind"] == "nonpatient" for c in claims):
        issues.append("Documented collateral-only portion excluded from patient contact")

    def select(kind, precedence, source_claims=claims):
        values, sources = choose(source_claims, kind, precedence)
        if sources:
            used[kind] = [{k: c[k] for k in ("document_id", "path", "line", "role", "value", "date")} for c in sources]
        return values

    status = select("attendance", ({"correction"}, {"roster", "signed"}))
    # Multiple signed positive and negative sources create an unresolved fact.
    positives = {"attended"}
    negatives = {"absent", "cancelled", "excluded_service"}
    if service != "unknown" and service not in THERAPY:
        event["attendance"] = "ineligible"
    elif status and set(status) <= negatives:
        event["attendance"] = "not_attended"
    elif status and set(status) <= positives and event["date"] and service in THERAPY:
        event["attendance"] = "attended"
    if event["attendance"] in {"ineligible", "not_attended"}:
        event.update(minute_min=0, minute_max=0, minute_values=[0], interval_options=[[]])
        return event
    if set(status) & negatives and set(status) & positives:
        issues.append("Conflicting signed attendance evidence")
    if event["attendance"] == "unresolved":
        issues.append("Attendance unresolved")
        if any(c["role"] == "draft" and c["kind"] == "contact" for c in claims):
            issues.append("Draft patient-contact interval could be confirmed by the signed provider note")
    clocks = select("contact", ({"correction"}, {"signed", "roster"}))
    candidate_only = False
    if not clocks and event["attendance"] == "unresolved":
        clocks = select("contact", ({"draft", "unverified"},))
        candidate_only = bool(clocks)
    group_no_break = False
    if not clocks:
        arrival = select("arrival", ({"correction"}, {"roster"}, {"signed"}))
        departure = select("departure", ({"correction"}, {"roster"}, {"signed"}))
        if len(arrival) > 1 or len(departure) > 1:
            issues.append("Conflicting patient clock times")
        # Corrections and independent attendance fields can be combined. Coequal whole
        # contact intervals above are preserved as units to avoid inventing pairings.
        clocks = [[[low, high]] for low, high in itertools.product(arrival, departure) if low < high]
        if service == "group" and clocks:
            schedule = select("scheduled", ({"signed", "roster"},))
            if not schedule:
                schedule = select("scheduled", ({"group_default"},), global_group_rules)
            if len(schedule) == 1:
                span = schedule[0][0]
                clocks = [[[max(low, span[0]), min(high, span[1])]] for [[low, high]] in clocks]
                clocks = [c for c in clocks if c[0][0] < c[0][1]]
            breaks = select("break", ({"signed", "roster"},))
            if not breaks:
                related = [c for c in global_group_rules if c["kind"] == "break"]
                if len(schedule) == 1:
                    scheduled_options = defaultdict(list)
                    for c in global_group_rules:
                        if c["kind"] == "scheduled":
                            scheduled_options[c["path"]].append(c["value"])
                    related = [c for c in related if schedule[0] in scheduled_options[c["path"]]]
                if len(unique(c["value"] for c in related)) == 1:
                    breaks = select("break", ({"group_default"},), related)
            if breaks:
                clocks = [subtract(times, break_slots) for times, break_slots in itertools.product(clocks, breaks)]
            else:
                group_no_break = True
                issues.append("Group break policy missing or ambiguous")
    if clocks:
        if len(clocks) > 1:
            issues.append("Alternative patient-contact intervals retained")
        minute_values = sorted({duration(spans) for spans in clocks})
        if event["attendance"] == "unresolved":
            clocks = [[]] + clocks
            minute_values = sorted({0, *minute_values})
        event.update(minute_min=0 if group_no_break else min(minute_values), minute_max=max(minute_values),
                     minute_values=[] if group_no_break else minute_values,
                     interval_options=[] if group_no_break else unique(clocks))
        if candidate_only:
            event["draft_only_values"] = minute_values
            event["minute_max"] = None
    else:
        issues.append("Actual patient-contact minutes undocumented")
    direct = used.get("contact", ())
    # A signed date and its actual interval belong to the same alternative.
    # Independent min/max projection would allow combinations no source supports.
    if (service in THERAPY and dated and set(status) & positives and direct
            and all(c["role"] in {"signed", "roster", "correction"} for c in direct)
            and {c["date"] for c in direct} == set(dated)):
        variants = defaultdict(list)
        for c in direct:
            variants[(c["date"], tuple(map(tuple, c["value"])))].append(
                {name: c[name] for name in ("document_id", "path", "line")})
        event["joint_options"] = [
            {"date": day, "intervals": [list(span) for span in spans], "evidence": evidence}
            for (day, spans), evidence in sorted(variants.items())
        ]
        if set(status) & negatives:
            event["joint_options"].append({
                "date": None, "intervals": [],
                "evidence": [{name: c[name] for name in ("document_id", "path", "line")}
                             for c in used["attendance"] if c["value"] in negatives]})
    return event


def group_claims(claims):
    by_event = defaultdict(list)
    appoint_to_encounter = defaultdict(set)
    group_rules = [c for c in claims if c["role"] == "group_default"]
    for c in claims:
        if c["appointment"] and c["encounter"]:
            appoint_to_encounter[c["appointment"]].add(c["encounter"])
    for c in claims:
        if c["kind"] in {"plan", "coverage", "episode", "assessment", "retrospective_summary"} or c["role"] == "group_default":
            continue
        key = c["encounter"]
        if key is None and c["appointment"]:
            links = appoint_to_encounter[c["appointment"]]
            key = next(iter(links)) if len(links) == 1 else "appointment:" + c["appointment"]
        if key is None and c["date"] and c["kind"] in {"attendance", "contact", "arrival", "departure", "break"}:
            key = f"unlinked:{c['document_id'] or c['path']}:{c['date']}"
        if key:
            by_event[key].append(c)
    return by_event, group_rules


def replay(claims):
    by_event, group_rules = group_claims(claims)
    return [reconcile_event(event_id, rows, group_rules) for event_id, rows in sorted(by_event.items())]


def assessments(claims):
    by_day = defaultdict(list)
    for c in claims:
        if c["kind"] == "assessment":
            by_day[(c["date"], c["value"]["instrument"])].append(c)
    results = []
    for (day, instrument), rows in sorted(by_day.items(), key=lambda pair: (pair[0][0] or "", pair[0][1])):
        by_form = defaultdict(list)
        for c in rows:
            by_form[c["value"].get("assessment_id")].append(c)
        for form, assertions in sorted(by_form.items(), key=lambda entry: entry[0] or ""):
            direct = sorted({c["value"]["score"] for c in assertions if c["role"] == "administered"})
            draft = sorted({c["value"]["score"] for c in assertions if c["role"] == "draft"})
            references = sorted({c["value"]["score"] for c in assertions if c["role"] == "retrospective"})
            results.append({
                "date": day, "instrument": instrument, "assessment_ids": [form] if form else [],
                "score": direct[0] if len(direct) == 1 and day else None,
                "status": "documented" if len(direct) == 1 and day else "conflicting" if len(direct) > 1 else "unverified",
                "administered_scores": direct, "draft_scores": draft,
                "retrospective_scores": references, "sources": assertions,
            })
    return results
