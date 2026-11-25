"""Content-addressed ingest and patient-local replay."""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sqlite3
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from extract import NO_SUPPORTED_CLAIMS, extract
from reconcile import group_claims, reconcile_event

EXTRACTOR_VERSION = "text-rules-4"
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(
    digest TEXT PRIMARY KEY, path TEXT NOT NULL, document_id TEXT, patient_id TEXT, patient TEXT,
    body TEXT NOT NULL, warnings TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aliases(path TEXT NOT NULL, digest TEXT NOT NULL REFERENCES documents(digest),
    PRIMARY KEY(path, digest));
CREATE TABLE IF NOT EXISTS claims(
    id INTEGER PRIMARY KEY, digest TEXT NOT NULL REFERENCES documents(digest),
    patient_id TEXT NOT NULL, date TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS claim_patient_date ON claims(patient_id, date, kind);
CREATE TABLE IF NOT EXISTS events(
    patient_id TEXT NOT NULL, event TEXT NOT NULL, date TEXT, payload TEXT NOT NULL, input_hash TEXT,
    PRIMARY KEY(patient_id, event)
);
CREATE INDEX IF NOT EXISTS event_dates ON events(patient_id, date);
CREATE INDEX IF NOT EXISTS event_collection_dates ON events(date, patient_id);
CREATE INDEX IF NOT EXISTS document_patient ON documents(patient_id);
CREATE TABLE IF NOT EXISTS patient_weeks(
    patient_id TEXT NOT NULL, monday TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(patient_id, monday)
);
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def database(path, create=False):
    path = Path(path)
    if not create and not path.is_file():
        raise ValueError(f"Saved abstraction not found: {path}")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    if create:
        conn.executescript(SCHEMA)
        if "input_hash" not in {row["name"] for row in conn.execute("PRAGMA table_info(events)")}:
            conn.execute("ALTER TABLE events ADD COLUMN input_hash TEXT")
        conn.execute("INSERT OR IGNORE INTO meta VALUES('extractor_version',?)", (EXTRACTOR_VERSION,))
        conn.commit()
    item = conn.execute("SELECT value FROM meta WHERE key='extractor_version'").fetchone()
    if item is None or item[0] != EXTRACTOR_VERSION:
        conn.close()
        raise ValueError("Extraction format changed. Ingest into a new database.")
    return conn


def read_source(path, raw):
    if path.suffix.lower() == ".pdf":
        if not shutil.which("pdftotext"):
            raise ValueError("PDF requires Poppler pdftotext in PATH")
        result = subprocess.run(["pdftotext", "-layout", "-", "-"], input=raw,
                                capture_output=True, timeout=60, check=True)
        extracted = result.stdout.decode("utf-8")
        if not extracted.strip():
            raise ValueError("PDF contains no extracted text. OCR the document and ingest its text with page markers.")
        pages = extracted.rstrip("\f").split("\f")
        return "\n".join(f"[PAGE {index}]\n{page.rstrip()}" for index, page in enumerate(pages, 1))
    if path.suffix.lower() == ".docx":
        try:
            with ZipFile(io.BytesIO(raw)) as zip_file:
                root = ElementTree.fromstring(zip_file.read("word/document.xml"))
        except (BadZipFile, KeyError, ElementTree.ParseError):
            raise ValueError("Invalid DOCX: expected a readable word/document.xml") from None
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        return "\n".join("".join(node.text or "" for node in p.findall(".//w:t", ns)) for p in root.findall(".//w:p", ns))
    return raw.decode("utf-8-sig")


def ingest(db, source):
    source = Path(source)
    if not source.is_dir():
        raise ValueError(f"Document directory missing: {source}")
    started = time.perf_counter()
    totals = {"files": 0, "new_documents": 0, "new_with_evidence": 0, "stored_without_evidence": [],
              "duplicate_files": 0, "cached_files": 0, "claims_added": 0,
              "patient_replays": 0, "encounters_recomputed": 0, "encounters_removed": 0,
              "weeks_recomputed": 0, "extraction_seconds": 0, "reconciliation_seconds": 0,
              "model_calls": 0, "model_tokens": 0, "model_api_cost_usd": 0, "errors": [], "warnings": []}
    touched = defaultdict(list)
    with db:
        # One writer per database. Failed files are reported and can be retried.
        db.execute("BEGIN IMMEDIATE")
        for path in sorted(source.rglob("*")):
            if not path.is_file() or any(x.startswith(".") for x in path.relative_to(source).parts):
                continue
            totals["files"] += 1
            label = path.relative_to(source).as_posix()
            if path.suffix.lower() not in {".txt", ".md", ".pdf", ".docx"}:
                totals["errors"].append({"path": label, "error": "Unsupported file type"})
                continue
            before = time.perf_counter()
            try:
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                exists = db.execute("SELECT patient_id,warnings FROM documents WHERE digest=?", (digest,)).fetchone()
                if exists:
                    inserted = db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?)", (label, digest)).rowcount
                    totals["duplicate_files" if inserted else "cached_files"] += 1
                    if not exists["patient_id"] or NO_SUPPORTED_CLAIMS in json.loads(exists["warnings"]):
                        totals["stored_without_evidence"].append({
                            "path": label, "reason": "missing_patient_id" if not exists["patient_id"] else "no_supported_claims",
                            "reused": True,
                        })
                    continue
                body = read_source(path, raw)
                evidence = extract(label, body)
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as err:
                totals["errors"].append({"path": label, "error": str(err)})
                continue
            totals["extraction_seconds"] += time.perf_counter() - before
            patient_id = evidence["patient_id"]
            doc_id = evidence["document_id"]
            db.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?)",
                       (digest, label, doc_id, patient_id, evidence["patient"], body, encode(evidence["warnings"])))
            db.execute("INSERT INTO aliases VALUES(?,?)", (label, digest))
            for claim in evidence["claims"]:
                claim.update(document_id=doc_id, path=label, digest=digest)
                db.execute("INSERT INTO claims(digest,patient_id,date,kind,payload) VALUES(?,?,?,?,?)",
                           (digest, patient_id, claim["date"], claim["kind"], encode(claim)))
            if evidence["claims"]:
                touched[patient_id].extend(c for c in evidence["claims"] if c["kind"] in {"plan", "coverage", "episode"})
                totals["new_with_evidence"] += 1
            else:
                totals["stored_without_evidence"].append({
                    "path": label, "reason": "missing_patient_id" if not patient_id else "no_supported_claims",
                    "reused": False,
                })
            totals["new_documents"] += 1
            totals["claims_added"] += len(evidence["claims"])
            if evidence["warnings"]:
                totals["warnings"].append({"path": label, "warnings": evidence["warnings"]})
        started_replay = time.perf_counter()
        for patient_id, policy_changes in sorted(touched.items()):
            recompute_patient(db, patient_id, policy_changes, totals)
        totals["reconciliation_seconds"] = time.perf_counter() - started_replay
        totals["patient_replays"] = len(touched)
    totals["elapsed_seconds"] = time.perf_counter() - started
    return totals


def recompute_patient(db, patient_id, policy_changes, totals):
    from review import refresh_weeks

    # Grouping is patient-local. An input fingerprint suppresses reconciliation
    # when the membership and all shared inputs of an encounter are unchanged.
    grouped, group_rules = group_claims(get_claims(db, patient_id))
    existing = {row["event"]: row for row in db.execute(
        "SELECT event,date,payload,input_hash FROM events WHERE patient_id=?", (patient_id,))}
    shared = sorted(encode(c) for c in group_rules)
    changed_dates = set()
    for event_id, old in existing.items():
        if event_id not in grouped:
            changed_dates.update(json.loads(old["payload"])["date_candidates"] or [None])
            db.execute("DELETE FROM events WHERE patient_id=? AND event=?", (patient_id, event_id))
            totals["encounters_removed"] += 1
    for event_id, claims in sorted(grouped.items()):
        group_inputs = shared if any(c["service"] == "group" for c in claims) else []
        fingerprint = hashlib.sha256(encode([sorted(encode(c) for c in claims), group_inputs]).encode()).hexdigest()
        old = existing.get(event_id)
        if old is not None and old["input_hash"] == fingerprint:
            continue
        event = reconcile_event(event_id, claims, group_rules)
        if old is not None:
            changed_dates.update(json.loads(old["payload"])["date_candidates"] or [None])
        changed_dates.update(event["date_candidates"] or [None])
        db.execute("""INSERT INTO events(patient_id,event,date,payload,input_hash) VALUES(?,?,?,?,?)
                      ON CONFLICT(patient_id,event) DO UPDATE SET date=excluded.date,
                      payload=excluded.payload,input_hash=excluded.input_hash""",
                   (patient_id, event_id, event["date"], encode(event), fingerprint))
        totals["encounters_recomputed"] += 1
    totals["weeks_recomputed"] += refresh_weeks(db, patient_id, changed_dates, policy_changes)


def get_claims(db, patient_id, kinds=None):
    sql = "SELECT payload FROM claims WHERE patient_id=?"
    params = [patient_id]
    if kinds:
        sql += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
        params.extend(kinds)
    return [json.loads(row[0]) for row in db.execute(sql, params)]


def get_events(db, patient_id, start, end):
    sql = """SELECT payload FROM events WHERE patient_id=? AND (date BETWEEN ? AND ? OR date IS NULL)
             ORDER BY date,event"""
    events = [json.loads(row[0]) for row in db.execute(sql, (patient_id, start, end))]
    return [e for e in events if e["date"] or not e["date_candidates"] or
            any(start <= day <= end for day in e["date_candidates"])]


def patients(db):
    result = {}
    for key, name in db.execute("SELECT DISTINCT patient_id,patient FROM documents WHERE patient_id IS NOT NULL ORDER BY patient_id,patient"):
        result.setdefault(key, []).append(name)
    return result


def find_patient(db, query):
    aliases = patients(db)
    direct = [key for key, names in aliases.items() if query.casefold() in {x.casefold() for x in [key, *names]}]
    if len(direct) == 1:
        return direct[0]
    contains = [key for key, names in aliases.items()
                if any(re.search(r"(?i)\b" + re.escape(query) + r"\b", name) for name in names)]
    if len(contains) != 1:
        raise ValueError(f"Patient name or identifier not unique in abstraction: {query}")
    return contains[0]


def dump(db):
    return {
        "patients": patients(db),
        "documents": [dict(row) for row in db.execute(
            "SELECT digest,path,document_id,patient_id,patient,warnings FROM documents ORDER BY path")],
        "source_aliases": [dict(row) for row in db.execute("SELECT path,digest FROM aliases ORDER BY path,digest")],
        "claims": [json.loads(row[0]) for row in db.execute("SELECT payload FROM claims ORDER BY id")],
        "events": [{"patient_id": row[0], **json.loads(row[1])}
                   for row in db.execute("SELECT patient_id,payload FROM events ORDER BY patient_id,date,event")],
    }
