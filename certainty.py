"""Prove weekly thresholds over mutually exclusive encounter interpretations."""
from __future__ import annotations

from math import ceil

MAX_STATES = 4096
ALGORITHM_VERSION = 2


def _source(claim):
    return {key: claim[key] for key in ("document_id", "path", "line")}


def _evidence(event, names):
    entries = {}
    for name in names:
        for claim in event["decision_sources"].get(name, ()):
            entries[(claim["path"], claim["line"])] = _source(claim)
    return [entries[key] for key in sorted(entries)]


def _choices(event, start, end):
    if event["attendance"] in {"ineligible", "not_attended"}:
        return ()
    options = event.get("joint_options")
    if options is None and event["date"] and event["interval_options"] and event["minute_max"] is not None:
        options = [
            {"date": event["date"] if spans or event["attendance"] == "attended" else None,
             "intervals": spans, "evidence": _evidence(event, ("contact", "arrival", "departure",
                                                             "attendance", "scheduled", "break"))}
            for spans in event["interval_options"]
        ]
    if not options:
        return None
    return [
        {**option, "event": event["event"]} if option["date"] and start <= option["date"] <= end else
        {**option, "event": event["event"], "date": None, "intervals": []}
        for option in options
    ]


def _minute_mask(intervals):
    mask = 0
    for start, end in intervals:
        mask |= ((1 << (end - start)) - 1) << start
    return mask


def _witnesses(events, start, end, goal_days, goal_minutes):
    alternatives = []
    for event in events:
        choices = _choices(event, start, end)
        if choices is None:
            return None, "unknown encounter timing or disposition"
        if choices:
            alternatives.append(choices)
    prepared = [[(choice, _minute_mask(choice["intervals"])) for choice in choices]
                for choices in alternatives]
    remaining = {}
    future_masks = [{} for _ in prepared]
    for index in reversed(range(len(prepared))):
        future_masks[index] = remaining.copy()
        for choice, mask in prepared[index]:
            if day := choice["date"]:
                remaining[day] = remaining.get(day, 0) | mask

    # Preserve clock bits only while a later alternative can overlap them.
    # An open attendance day stays in the state even after all its current
    # minutes are finalized, so a later visit cannot count that day twice.
    states = {(0, 0, ()): (0, 0, ())}
    goal_minutes = ceil(goal_minutes)
    for choices, future in zip(prepared, future_masks):
        next_states = {}
        for (_, _, active), (count, minutes, trace) in states.items():
            for choice, mask in choices:
                days = dict(active)
                day = choice["date"]
                if day:
                    days[day] = days.get(day, 0) | mask
                live = {}
                updated_count, updated_minutes = count, minutes
                for active_day, occupied in days.items():
                    if active_day in future:
                        live[active_day] = occupied & future[active_day]
                        updated_minutes += (occupied & ~future[active_day]).bit_count()
                    else:
                        updated_count += 1
                        updated_minutes += occupied.bit_count()
                signature = (min(updated_count, goal_days), min(updated_minutes, goal_minutes),
                             tuple(sorted(live.items())))
                if signature not in next_states:
                    next_states[signature] = (updated_count, updated_minutes, trace + (choice,))
                    if len(next_states) > MAX_STATES:
                        return None, "joint scenario search limit"
        states = next_states
    witnesses = {}
    for count, minutes, trace in states.values():
        outcome = "pass" if count >= goal_days and minutes >= goal_minutes else "fail"
        witnesses.setdefault(outcome, {"days": count, "minutes": minutes, "events": list(trace)})
        if len(witnesses) == 2:
            break
    return witnesses, None


def determine(events, total, plan, coverage, complete, start, end):
    """Return a verdict and the proof used at this saved evidence revision."""
    proof = {
        "algorithm_version": ALGORITHM_VERSION, "method": "missing plan", "pass_possible": None, "fail_possible": None,
        "target": plan["value"] if plan else None, "plan_source": _source(plan) if plan else None,
        "coverage_source": _source(coverage) if coverage else None, "complete": complete,
        "bounds": {"days": [total["days_confirmed"], total["days_possible_upper"]],
                   "minutes": [total["minute_min"], total["minute_max"]]},
        "witnesses": {},
    }
    if not plan:
        return "cannot determine", proof
    days, minutes = plan["value"]["days"], plan["value"]["minutes"]
    low_d, high_d = proof["bounds"]["days"]
    low_m, high_m = proof["bounds"]["minutes"]
    if low_d >= days and low_m >= minutes:
        # Known delivery is sufficient even if more activity could be missing.
        proof.update(method="guaranteed lower bounds", pass_possible=True, fail_possible=False)
        return "met", proof
    if complete and ((high_d is not None and high_d < days) or (high_m is not None and high_m < minutes)):
        proof.update(method="exhaustive upper bounds", pass_possible=False, fail_possible=True)
        return "not met", proof
    witnesses, limitation = _witnesses(events, start, end, days, minutes)
    if witnesses is None:
        proof.update(method=limitation, bounds_inconclusive=True)
        return "cannot determine", proof
    proof.update(method="joint scenarios", witnesses=witnesses,
                 pass_possible="pass" in witnesses, fail_possible="fail" in witnesses)
    if "fail" not in witnesses:
        return "met", proof
    if "pass" in witnesses:
        return "cannot determine", proof
    if complete:
        return "not met", proof
    proof.update(method="incomplete coverage", pass_possible=None)
    return "cannot determine", proof
