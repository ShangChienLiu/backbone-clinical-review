"""Extract source-anchored claims from text clinical records."""
from __future__ import annotations

import calendar
import re
from datetime import date
from pathlib import Path

MONTHS = "January February March April May June July August September October November December".split()
MONTH = r"\b(" + "|".join(MONTHS + [m[:3] for m in MONTHS]) + r")\.?\s*(\d{1,2})(?:,?\s+(20\d{2}))?\b"
ISO = r"\b(20\d{2})-(\d{2})-(\d{2})\b"
US = r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(20\d{2}|\d{2}))?\b"
TIME = r"\b(\d{1,2}:\d{2})(?:\s*(AM|PM)\b)?"
RANGE = TIME + r"\s*(?:[-–—]|to|through)\s*" + TIME
IDENTITY = r"(?i)\b(?:MRN|medical record\s*#)\s*:?\s*([\w-]+)"
ENCOUNTER = r"(?i)\b(?:encounter|visit|related appointment)\s*:?\s*([A-Z]{2,}(?:-[A-Z0-9]+)+\d)\b"
APPOINTMENT = r"(?i)\bAppointment(?:\s+ID)?\s*:?\s*([A-Z]{2,}(?:-[A-Z0-9]+)+\d)\b"
ASSESSMENT = r"\b(PHQ[- ]?9|GAD[- ]?7)\b"
THERAPY = {"group", "individual", "family", "other_therapy"}
NO_SUPPORTED_CLAIMS = "No supported clinical claim extracted"


def days(text, year=None):
    candidates = []
    for pattern in (MONTH, ISO, US):
        for match in re.finditer(pattern, text, re.I):
            parts = match.groups()
            if pattern == MONTH:
                month = next(i + 1 for i, name in enumerate(MONTHS) if name.casefold().startswith(parts[0].casefold()))
                day, y = parts[1:]
            elif pattern == ISO:
                y, month, day = parts
            else:
                month, day, y = parts
            if y is not None and len(y) == 2:
                y = "20" + y
            try:
                candidates.append((match.start(), date(int(y or year), int(month), int(day)).isoformat()))
            except (ValueError, TypeError):
                continue
    return [value for _, value in sorted(candidates)]


def clock(value, meridian=None):
    hour, minute = (int(part) for part in value.split(":"))
    if meridian:
        if not 1 <= hour <= 12:
            raise ValueError(value)
        hour = hour % 12 + (12 if meridian.upper() == "PM" else 0)
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError(value)
    return hour * 60 + minute


def time_in(text, meridian=None):
    match = re.search(TIME, text, re.I)
    return clock(match[1], match[2] or meridian) if match else None


def intervals(text, meridian=None):
    text = re.sub(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.\s*m\.", 
                  lambda m: f"{m[1]}:{m[2] or '00'} {m[3].upper()}M", text, flags=re.I)
    result = []
    for start, start_marker, end, end_marker in re.findall(RANGE, text, re.I):
        low = clock(start, start_marker or end_marker or meridian)
        high = clock(end, end_marker or start_marker or meridian)
        if high < low and not start_marker and end_marker:
            low -= 720
        if 0 <= low < high <= 1440:
            result.append([low, high])
    return result


def service(text):
    low = text.casefold()
    for name, pattern in (
        ("coordination", r"care coordination|collateral|between professionals"),
        ("medication", r"medication (?:management|evaluation)|psychiatric evaluation|prescriber visit"),
        ("family", r"\bfamily (?:therapy|psychotherapy|session|service)"),
        ("group", r"\bgroup\b"),
        ("individual", r"\bindividual\b|\bparticipating clinician psychotherapy"),
        ("other_therapy", r"\bpsychotherapy\b"),
    ):
        if re.search(pattern, low):
            return name
    return "unknown"


def disposition(text):
    if re.search(r"no status|not finali[sz]ed|signature pending", text, re.I):
        return "unknown"
    if re.search(r"no[- ]show|not present|did not attend|absent", text, re.I):
        return "absent"
    if re.search(r"cancell?ed|did not meet", text, re.I):
        return "cancelled"
    if re.search(r"attended|\bkept\b|\bpresent\b|\bcompleted\b|\[X\]\s*Yes", text, re.I):
        return "attended"
    return "unknown"


def match_value(pattern, text):
    match = re.search(pattern, text, re.I)
    return match[1] if match else None


def display_name(value):
    value = re.sub(r'\s*\([^)]*\)', '', value).strip()
    if ',' in value:
        first, _, last = value.partition(',')
        value = f"{last.strip()} {first.strip()}"
    return value.title() if value.isupper() else value


def identity(lines):
    for index, line in enumerate(lines):
        mrn = match_value(IDENTITY, line)
        if not mrn:
            continue
        # The demographic row often follows a labeled name on the line above.
        for candidate in (line, lines[index - 1] if index else ""):
            name = re.split(r"\s*\||\s+-\s+(?:DOB|birth date)\b|\s*\([^)]*\bDOB\b", candidate, maxsplit=1, flags=re.I)[0].strip()
            name = re.sub(r"^(?:Patient(?: chart| copy)?|Client|Identified client|Pt|Member|Name|Chart routing|Re)\s*:\s*", "", name, flags=re.I)
            if name and not re.match(r"(?i)(?:DOB|birth date|date of birth|record |doc |visit |form )\b", name):
                name = re.split(r"\s*,?\s*\bMRN\b", name, maxsplit=1, flags=re.I)[0]
                return mrn.upper(), display_name(name)
        return mrn.upper(), mrn.upper()
    return None, None


def extract(name: str, text: str):
    lines = text.splitlines()
    nonempty = [(i + 1, x.strip()) for i, x in enumerate(lines) if x.strip()]
    header = "\n".join(lines[:8])
    yr = match_value(r"\b(20\d{2})\b", re.sub(r"(?i)\b(?:DOB|date of birth|birth date)\s*:[^|\n]*(?=\||\n|$)", "", header))
    if not yr:
        yr = match_value(r"\b(20\d{2})[-_]", Path(name).stem)
    if not yr:
        yr = match_value(r"\b(20\d{2})\b", text)
    mrn, patient = identity(lines)
    document_id = match_value(r"(?m)^(?:Document\s+ID|Record(?:\s+ID)?|Form ID|Note(?: ID)?|Register ID|Report ID|Reference|Doc(?: ID)?|ID|Document)\s*:\s*([A-Z]+-[A-Z0-9]+)", "\n".join(lines[:14]))
    if not document_id:
        document_id = match_value(r"^([A-Z]+-[A-Z0-9]+)[ _-]", Path(name).stem)
    result = {"patient_id": mrn, "patient": patient, "document_id": document_id, "claims": [], "warnings": []}
    if not mrn:
        result["warnings"].append("Missing explicit patient identifier")
        return result

    plan = bool(re.search(r"(?im)^\s*(?:INDIVIDUALIZED TREATMENT PLAN|MASTER TREATMENT PLAN|TREATMENT PLAN UPDATE|OUTPATIENT TREATMENT PLAN)|\| Outpatient treatment plan", header))
    plan_review = "treatment plan review" in header.casefold()
    attached = bool(re.search(r"(?im)^.*(?:inbox cover sheet|inbound document index sheet|transmission notice:.*retransmission)", header))
    admin = bool(re.search(r"(?im)^.*(?:administrative chart extract|charge reconciliation worksheet)", header))
    form = bool(re.search(r"(?im)^.*(?:attendance roster extract|sign-in sheet)", header)) and not attached
    amendment = bool(re.search(r"(?im)^.*(?:attendance correction|attendance amendment)", header))
    clinical_addendum = bool(re.search(r"(?im)^.*(?:clinical addendum|psychotherapy addendum|patient.contact correction|addendum / correction)", header))
    early_desk = bool(re.search(r"(?i)Appointment desk", header))
    early_group = bool(re.search(r"(?i)Group desk attendance extract", header))
    register = early_desk or early_group or bool(re.search(r"(?i)disposition extract|disposition report|client visit register|final attendance register", header))
    incomplete_register = re.search(
        r"\b(?:partial (?:attendance )?(?:extract|export|register)|not (?:a )?complete (?:attendance )?(?:extract|export|register)|selected (?:appointments|encounters|visits) only)\b",
        text, re.I,
    )
    questionnaire = bool(re.search(r"(?im)\| Measurement review|Symptom questionnaire and chart review|^Form:\s*" + ASSESSMENT, header))
    imported_measure = bool(re.search(r"(?i)administrative import receipt", header))
    auxiliary = bool(re.search(r"(?i)program operations|scheduling support log|administrative correspondence", header))
    if not admin and re.search(r"(?i)administrative correspondence", header):
        admin = True
    draft = bool(re.search(r"(?im)^.*(?:note status: in progress|draft\s*[-—]\s*unsigned|^unsigned(?:\s+draft)?\b)", text)) and not admin
    doc_service = service("\n".join(lines[:6]))
    if doc_service == "other_therapy":
        doc_service = service("\n".join(lines[:9]))
    if auxiliary and re.search(r"(?is)\bgroup scheduled\b.*?\bcancelled by the clinic\b", text):
        doc_service = "group"
    status = "draft" if draft else "signed" if re.search(r"(?im)\bsign(?:ed|atures)\b|record status: final", text) else "unverified"
    doc_date = None
    encounter = None
    appointment = match_value(APPOINTMENT, header)
    for number, line in nonempty[:15]:
        day_list = days(line, yr)
        if day_list and re.search(r"(?i)^patient:|\bDOB:|MRN:|^received:|^registrar|^document ID", line):
            continue
        if day_list and (match_value(ENCOUNTER, line) or re.search(r"(?i)service date|date of service|DOS:|^date:|amends:|completed by patient|questionnaire completed|notice entered:|^(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)\b|^(?:" + "|".join(MONTHS) + r")\b", line)):
            doc_date = day_list[0]
            break
    first_encounter = match_value(ENCOUNTER, "\n".join(lines[:8]))
    if (form or amendment or admin or attached) and not doc_date:
        for _, line in nonempty[:16]:
            if match_value(ENCOUNTER, line) and (parsed := days(line, yr)):
                doc_date = parsed[0]
                break

    def emit(number, quote, kind, value, *, on=None, event=None, typ=None, role=None):
        result["claims"].append({"line": number, "quote": quote, "kind": kind, "value": value,
            "date": on if on is not None else doc_date,
            "encounter": event if event is not None else encounter,
            "appointment": appointment,
            "service": typ or doc_service, "role": role or status})

    in_batch = False
    current_date = doc_date
    encounter = first_encounter
    time_marker = None
    amend_kind = None
    effective_date = None
    measure_context = False
    register_events = {}
    questionnaire_id = match_value(r"\b(?:form|questionnaire)\s+([A-Z]+-Q\d+)\b", header)
    if questionnaire and questionnaire_id is None and re.fullmatch(r"[A-Z]+-Q\d+", document_id or ""):
        questionnaire_id = document_id
    form_instrument = match_value(r"\bInstrument:\s*(" + ASSESSMENT + r")", header)
    if form_instrument is None and questionnaire:
        form_instrument = match_value(ASSESSMENT, "\n".join(lines[:12]))
    timed_header = next(((n, h, intervals(h)) for n, h in nonempty[:9] if match_value(ENCOUNTER, h) and intervals(h)), None)
    for number, line in nonempty:
        low = line.casefold()
        values = [part.strip() for part in line.split("|")]
        match_encounter = match_value(ENCOUNTER, line)
        parsed_days = days(line, yr)
        if early_desk and len(values) == 5 and re.fullmatch(r"[A-Z]+-[A-Z0-9]+\d", values[0]) and days(values[1], yr):
            event, on, typ = values[0], days(values[1], yr)[0], service(values[2])
            emit(number, line, "attendance", disposition(values[4]), on=on, event=event, typ=typ, role="roster")
            if planned := intervals(values[3]):
                emit(number, line, "scheduled", planned, on=on, event=event, typ=typ, role="roster")
            continue
        if early_group and len(values) == 6 and days(values[0], yr) and re.fullmatch(r"[A-Z]+-[A-Z0-9]+\d", values[1]):
            on, event = days(values[0], yr)[0], values[1]
            emit(number, line, "attendance", disposition(values[5]), on=on, event=event, typ="group", role="roster")
            if planned := intervals(values[2]):
                emit(number, line, "scheduled", planned, on=on, event=event, typ="group", role="roster")
            for value, kind in zip(values[3:5], ("arrival", "departure")):
                if (minute := time_in(value)) is not None:
                    emit(number, line, kind, minute, on=on, event=event, typ="group", role="roster")
            continue
        # Register rows directly assert patient attendance, with optional actual group clock times.
        if register and len(values) >= 7 and days(values[0], yr) and re.fullmatch(r"[A-Z]+-[A-Z0-9]+\d", values[1]):
            on = days(values[0], yr)[0]
            event, typ = values[1], service(values[2])
            register_events.setdefault(on, []).append((event, typ))
            emit(number, line, "attendance", disposition(values[6]), on=on, event=event, typ=typ, role="roster")
            scheduled = intervals(values[3])
            if scheduled:
                emit(number, line, "scheduled", scheduled, on=on, event=event, typ=typ, role="roster")
            for value, kind in zip(values[4:6], ("arrival", "departure")):
                if (minute := time_in(value)) is not None:
                    emit(number, line, kind, minute, on=on, event=event, typ=typ, role="roster")
            continue
        # A date plus encounter at a section boundary identifies a distinct service in a batch.
        if match_encounter and parsed_days and re.match(r"(?i)(?:DOS:|\d|(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|January|February|March|April|May|June|July|August|September|October|November|December)\b)", line):
            current_date, encounter = parsed_days[0], match_encounter
            in_batch = True
        elif match_encounter and (not encounter or attached or amendment):
            encounter = match_encounter
        on = current_date
        if attached and not on and (parsed := days(line, yr)) and re.search(r"(?i)re: payer|Date:", line):
            current_date = on = parsed[0]
        if register and parsed_days and re.search(r"(?i)^.{0,35}(?:attendance attestation|scheduling entry):", line):
            dated_events = register_events.get(parsed_days[0], [])
            if len(dated_events) == 1:
                event, typ = dated_events[0]
                emit(number, line, "context", "attendance attestation", on=parsed_days[0], event=event, typ=typ, role="roster")
        is_group_header = doc_service == "group" and not in_batch
        if plan and re.search(r"(?i)plan effective|plan start\s*\(effective\)|changes effective|effective date\s*:|episode dates:", line) and parsed_days:
            effective_date = parsed_days[0]
            if "episode dates:" in low and len(parsed_days) >= 2:
                emit(number, line, "episode", parsed_days[:2], on=effective_date, role="plan")
        if plan and effective_date and re.search(r"\b(?:week|weekly)\b", line, re.I):
            dm = re.search(r"\b(\d+)\s*(?:or more\s+)?(?:separate\s+|distinct\s+|therapy\s+)?days?\b", line, re.I)
            mm = re.search(r"\b(\d+(?:\.\d+)?)\s*(minute|hour)s?\b", line, re.I)
            if dm and mm:
                amount = float(mm[1]) * (60 if mm[2].lower() == "hour" else 1)
                emit(number, line, "plan", {"days": int(dm[1]), "minutes": amount}, on=effective_date, role="plan")
        if register and not incomplete_register and status == "signed" and "period" in low and len(parsed_days) >= 2:
            emit(number, line, "coverage", parsed_days[-2:], on=parsed_days[-2], role="roster")
        # The desk's range describes the appointments included by the export.
        if early_desk and not incomplete_register and low.startswith("view: appointments"):
            range_match = re.search(r"(\w+)\s+(\d{1,2})[–—-](\d{1,2}),?\s+(20\d{2})", line)
            if range_match:
                month, first, last, year = range_match.groups()
                parsed_period = days(f"{month} {first}, {year} to {month} {last}, {year}")
                if len(parsed_period) == 2 and parsed_period[0] <= parsed_period[1]:
                    emit(number, line, "coverage", parsed_period, on=parsed_period[0], role="roster")
        if register and not incomplete_register and "visit register" in low and (month := re.search(r"(?i)\b(" + "|".join(MONTHS) + r")\s+(20\d{2})\b", line)):
            first = date(int(month[2]), MONTHS.index(month[1].title()) + 1, 1)
            last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
            emit(number, line, "coverage", [first.isoformat(), last.isoformat()], on=first.isoformat(), role="roster")
        rs = intervals(line)
        if "format for all dates" in low:
            start = re.search(r"opens at|opens", line, re.I)
            end = re.search(r"closes at|closes", line, re.I)
            lo, hi = (time_in(line[m.end():]) if m else None for m in (start, end))
            rs = [[lo, hi]] if lo is not None and hi is not None and lo < hi else []
        if rs and re.search(r"(?i)scheduled group|group scheduled|standing schedule|format for all dates|group meets", line):
            emit(number, line, "scheduled", rs[:1], on=on, event=encounter, role="group_default" if is_group_header and len({x for x in re.findall(ENCOUNTER, text, re.I)}) > 1 else status)
            time_marker = "PM" if rs[0][0] >= 12 * 60 else None
        if doc_service == "group" and "break" in low and re.search(r"nontherapeutic|unstructured|free time|no group process|no therapy|no therapeutic|no facilitated", low):
            segment = line[low.index("break"):]
            br = intervals(segment, time_marker)
            if not br:
                duration = match_value(r"(\d+)\s*(?:-|\s)\s*minute", line)
                start = time_in(segment, time_marker)
                if duration and start is not None:
                    br = [[start, start + int(duration)]]
            if br:
                role = "group_default" if is_group_header and len(set(re.findall(ENCOUNTER, text, re.I))) > 1 else status
                emit(number, line, "break", br, on=on, role=role)
        if amendment:
            correction_role = "correction" if status == "signed" else "unverified"
            if re.search(r"(?i)transfer record|check-in log|amendment is linked|correction applies only", line):
                emit(number, line, "context", "amendment scope or corroboration", on=on, role=correction_role)
            for pat, kind in ((r"(?:patient )?departure.*?\bis\s+(" + TIME + r")", "departure"),
                              (r"(?:patient )?arrival.*?\bremains\s+(" + TIME + r")", "arrival")):
                if low.startswith("correction:") and (raw := match_value(pat, line)):
                    emit(number, line, kind, time_in(raw), on=on, role=correction_role)
            if low.startswith("field amended:"):
                amend_kind = "arrival" if "time in" in low else "departure" if "time out" in low else None
            if low.startswith("corrected entry:") and (minute := time_in(line)) is not None:
                if amend_kind:
                    emit(number, line, amend_kind, minute, on=on, role=correction_role)
        if clinical_addendum and encounter and on and re.search(r"(?i)^(?:(?:corrected|revised|amended)\s+patient(?:[ -]contact| psychotherapy contact)|patient.contact correction)\s*:", line):
            if corrected := intervals(line):
                correction_role = "correction" if status == "signed" else "unverified"
                emit(number, line, "contact", corrected, on=on, role=correction_role)
        if form or attached:
            for pat, kind in ((r"\b(?:patient arrival|time in)\s*:\s*([^|]+)", "arrival"),
                              (r"\b(?:patient departure|time out)\s*:\s*([^|]+)", "departure")):
                if (raw := match_value(pat, line)) and (minute := time_in(raw)) is not None:
                    emit(number, line, kind, minute, on=on, typ=doc_service if attached else "group", role="copy" if attached else "roster")
            if re.search(r"\bstatus:\s*Attended|Present:\s*\[X\]\s*Yes", line, re.I):
                emit(number, line, "attendance", "attended", on=on, typ="group", role="copy" if attached else "roster")
            if attached and re.search(r"retransmission|indexed to the date|no additional visit|only the sign-in sheet", low):
                emit(number, line, "administrative", "retransmission", on=on, typ=doc_service, role="copy")
        if admin and re.search(r"draft|unsigned|posted|charge|template attendance|pre-populat|billing", low):
            emit(number, line, "administrative", "billing or draft", on=on, role="administrative")

        if encounter and on and auxiliary and re.search(r"(?i)marked no show|scheduled.*cancelled by the clinic|no group was held", line):
            emit(number, line, "attendance", "absent" if "no show" in low else "cancelled", on=on, role="roster")
        if encounter and on and not (register or admin or form or attached or amendment or plan or plan_review or auxiliary):
            if doc_service in {"coordination", "medication"} and re.search(r"(?i)patient participation|client present|no psychotherapy add-on|no psychotherapy was provided|no patient contact occurred|no separately documented psychotherapy|no separate psychotherapy|no patient-present psychotherapy|was absent for the entire", line):
                emit(number, line, "attendance", "excluded_service", on=on)
            if doc_service == "group" and in_batch and re.match(r"(?i)not present|group did not meet|cancelled by", line):
                emit(number, line, "attendance", disposition(line), on=on)
            if doc_service == "group" and re.search(r"(?i)^the session addressed|^the session reviewed|session began.*present|present from|^on time; present|^checked in|\bengaged throughout|\bstayed for the whole group|^theme:|^topic:|\barrived at", line):
                emit(number, line, "attendance", "attended", on=on)
            if doc_service in THERAPY and (doc_service != "group" or re.match(r"(?i)^(?:actual )?patient contact\s*:", line)):
                contacts = []
                if re.search(r"(?i)^(?:actual patient psychotherapy contact|patient contact(?: time)?|client contact time|patient-present (?:session|individual therapy)|(?:the )?patient (?:was in|participated in|received) (?:individual )?(?:psychotherapy|therapy)|time in|session start|start\s+\d|session began|session with the client)", line):
                    segment = re.split(r"(?i)\bconnection was lost\b|\bthere was no\b", line, maxsplit=1)[0]
                    contacts = intervals(segment)
                    if not contacts and len(stamps := list(re.finditer(TIME, segment, re.I))) >= 2:
                        start, end = stamps[:2]
                        lo, hi = clock(start[1], start[2] or end[2]), clock(end[1], end[2] or start[2])
                        contacts = [[lo, hi]] if lo < hi else []
                elif "client contact time:" in low:
                    contacts = intervals(line[low.index("client contact time:"):])
                elif doc_service == "family" and re.search(r"\bpresent with\b|\btogether:", low):
                    contacts = intervals(re.split(r"(?i)partner only:[^.]*\.\s*", line)[-1])
                elif re.search(r"(?i)\bminutes with (?:the )?(?:patient|client) present\b", line):
                    contacts = intervals(line)
                if doc_service == "family" and timed_header and re.search(r"(?i)patient-present family therapy duration|both present for the full", line):
                    time_line, time_quote, contacts = timed_header
                    emit(time_line, time_quote,
                         "context", "clock interval corroborated by patient-presence statement", on=on)
                if contacts:
                    emit(number, line, "contact", contacts, on=on)
                    emit(number, line, "attendance", "attended", on=on)
                for phrase, kind in (("we ended the session at", "departure"), ("we started right at", "arrival")):
                    if phrase in low and (minute := time_in(line[low.index(phrase) + len(phrase):])) is not None:
                        emit(number, line, kind, minute, on=on)
                if re.search(r"(?i)^(?:partner only|.+ only:)|therapist (?:session interval|time)|consultation without", line):
                    if "only:" in low:
                        spans = intervals(re.split(r"[.;]", line, maxsplit=1)[0])
                    else:
                        spans = []
                    if spans:
                        emit(number, line, "nonpatient", spans, on=on)
            if re.search(r"(?i)^attached platform|^appointment ID\s*\||rejoin original appointment|after network disconnect|\bconnection was lost\b|\bno audio or video contact\b", line) or re.match(r"(?i)^\S+\s*\|\s*VC-", line):
                emit(number, line, "platform", "corroborates continuity or connection times", on=on)
        if plan_review and re.search(r"(?i)\bweekly minimum\b|\bPHQ-9\b|\bGAD-7\b|\bprogress\b", line):
            review_days = days(text.split("Attendance", 1)[0].split("Review completed:")[-1], yr)
            emit(number, line, "retrospective_summary", "later review", on=review_days[0] if review_days else on)
        if encounter and on and not (form or attached or admin or amendment or register or plan or plan_review):
            if re.search(r"(?i)\b(?:reported|described|discussed|became|anxious|anxiety|sleep|panic|avoidance|low mood|SI|depressive|coping|function|grounding|work calendar|mood|attacks|assessment|participated|drafting|scores?)\b", line) and len(line) > 45:
                emit(number, line, "observation", "clinical narrative", on=on)
        if questionnaire and on and len(line) > 70 and re.search(r"(?i)clinician review|continued to endorse|partial improvement|patient wrote", line):
            emit(number, line, "observation", "questionnaire narrative", on=on)
        if questionnaire and form_instrument and (score := match_value(r"(?:^Total score|\|\s*Total)\s*:\s*(\d{1,2})", line)):
            receipt_id = match_value(r"(?i)duplicate receipt of form ([A-Z]+-Q\d+)", text)
            role = "retrospective" if receipt_id else "draft" if draft else "administered"
            limit = 27 if form_instrument.upper() == "PHQ-9" else 21
            if int(score) <= limit:
                emit(number, line, "assessment", {"instrument": form_instrument.upper(), "score": int(score),
                                                "assessment_id": receipt_id or questionnaire_id}, on=on, role=role)
        if imported_measure and len(values) >= 4 and re.fullmatch(ASSESSMENT, values[0], re.I) and values[1].isdigit() and days(values[2], yr):
            emit(number, line, "assessment", {"instrument": values[0], "score": int(values[1]), "assessment_id": values[3]},
                 on=days(values[2], yr)[0], role="retrospective")
            continue
        if re.search(r"(?i)^standardized measures|^measures today:", line):
            measure_context = True
        if re.match(r"(?i)^[OAP]:", line):
            measure_context = False
        for found in re.finditer(ASSESSMENT, line, re.I):
            tail = line[found.end():]
            if re.search(r"(?i)\b(?:item\s*9|not repeated|under|below)\b", tail[:22]) and not re.match(r"(?i)^\s*(?:(?:today was|total)\s*)?[:=]?\s*\d+", tail):
                if not re.search(r"(?i)\btotal\s+\d+", tail):
                    continue
            score = match_value(r"^\s*(?:(?:today\s+)?(?:was|of)|total)?\s*(?:=|:)?\s*(\d{1,2})\b", tail)
            if score is None and "completed by" in tail:
                score = match_value(r"\btotal score\s+(\d{1,2})", tail)
            if score is None and (measure_context or "item scores:" in tail):
                score = match_value(r"\btotal\s+(\d{1,2})", tail) or score
            if score is None:
                continue
            kind = found[1].upper().replace(" ", "-").replace("PHQ9", "PHQ-9").replace("GAD7", "GAD-7")
            maximum = 27 if kind == "PHQ-9" else 21
            if int(score) > maximum:
                continue
            is_intake = plan and "intake" in low and bool(parsed_days)
            is_current = (questionnaire or measure_context or re.search(r"(?i)administered today|measures\s*(?:today|\(today\))|today was|scored in session|completed by.*total score", line)) and bool(on)
            source_role = "draft" if draft else "administered" if (is_intake or is_current) else "retrospective"
            remaining = re.split(ASSESSMENT, tail, maxsplit=1, flags=re.I)[0]
            historical_dates = days(remaining, yr)
            if plan_review:
                occurrences = re.findall(r"(\d{1,2})\s*\(([^)]+)\)", remaining)
                for value, when in occurrences:
                    if int(value) <= maximum and (dated := days(when, yr)):
                        emit(number, line, "assessment", {"instrument": kind, "score": int(value)}, on=dated[0], role="retrospective")
                if occurrences:
                    continue
            assessed = parsed_days[0] if is_intake else historical_dates[0] if source_role == "retrospective" and historical_dates else on
            emit(number, line, "assessment", {"instrument": kind, "score": int(score), "assessment_id": questionnaire_id}, on=assessed, role=source_role)
    if admin and "authorization" in text.casefold() and not result["claims"]:
        for number, line in nonempty:
            if "authorized quantity" in line.casefold():
                emit(number, line, "administrative", "authorization", role="administrative")
    if not result["claims"]:
        result["warnings"].append(NO_SUPPORTED_CLAIMS)
    elif (doc_service in THERAPY - {"group"} and first_encounter and status == "signed"
            and not (register or admin or form or attached or amendment or clinical_addendum or plan)
            and re.search(r"(?im)^.*(?:patient contact|patient.present|elapsed time|received .*therapy|participated in .*therapy)", text)
            and not any(c["kind"] in {"contact", "arrival", "departure"} for c in result["claims"])):
        result["warnings"].append("No clinical contact interval extracted from the signed note")
    return result
