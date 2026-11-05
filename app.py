from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import mimetypes
import os
import hmac
import re
import secrets
import sqlite3
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from xml.etree import ElementTree
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("LOAN_DESK_DATA_DIR", ROOT / "data"))
DATABASE_PATH = DATA_DIR / "loan_desk.sqlite3"
UPLOAD_DIR = DATA_DIR / "documents"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MCP_PROTOCOL_VERSION = "2025-03-26"
SESSION_TTL_SECONDS = 12 * 3600
SESSION_COOKIE = "folio_session"
SESSIONS: dict[str, tuple[str, float]] = {}
FAILED_LOGINS: dict[str, tuple[int, float]] = {}
AUTH_LOCK = threading.Lock()
_mcp_token_cache: str | None = None


def credentials() -> tuple[str, str]:
    return (
        os.environ.get("APP_USERNAME", "admin"),
        os.environ.get("APP_PASSWORD", "folio-demo"),
    )


def get_mcp_token() -> str:
    global _mcp_token_cache
    configured = os.environ.get("MCP_TOKEN")
    if configured:
        return configured
    if _mcp_token_cache is None:
        token_file = DATA_DIR / "mcp_token.txt"
        if token_file.is_file():
            _mcp_token_cache = token_file.read_text().strip()
        else:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _mcp_token_cache = secrets.token_urlsafe(24)
            token_file.write_text(_mcp_token_cache)
    return _mcp_token_cache


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    with AUTH_LOCK:
        now = time.time()
        for key in [k for k, (_, expires) in SESSIONS.items() if expires < now]:
            del SESSIONS[key]
        SESSIONS[token] = (username, now + SESSION_TTL_SECONDS)
    return token


def session_user(token: str | None) -> str | None:
    if not token:
        return None
    with AUTH_LOCK:
        entry = SESSIONS.get(token)
        if entry and entry[1] > time.time():
            return entry[0]
        SESSIONS.pop(token, None)
    return None
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".docx", ".xlsx", ".csv"}
STATUSES = {"New", "In review", "Needs info", "Approved", "Declined"}
DOCUMENT_CATEGORIES = {
    "Identity",
    "Income",
    "Bank statement",
    "Credit report",
    "Address proof",
    "Other",
}
FLAG_SEVERITIES = {"info", "warning", "critical"}
MAX_EXTRACTED_TEXT = 30000
MAX_EXTRACTED_FIELDS = 60
MAX_MCP_FILE_BYTES = 8 * 1024 * 1024
INLINE_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/webp"}
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
HAS_FILE_SQL = "(d.content IS NOT NULL OR d.file_path IS NOT NULL)"
EDITABLE_FIELDS = {
    "applicant_name",
    "email",
    "phone",
    "city",
    "loan_type",
    "amount",
    "purpose",
    "monthly_income",
    "credit_score",
    "status",
    "reviewer_note",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _xml_text(data: bytes, paragraph_tag: str, text_tag: str) -> list[str]:
    paragraphs = []
    for paragraph in ElementTree.fromstring(data).iter(paragraph_tag):
        text = "".join(node.text or "" for node in paragraph.iter(text_tag))
        if text.strip():
            paragraphs.append(text)
    return paragraphs


def extract_text(filename: str, content: bytes) -> str:
    """Deterministic text extraction for formats we can read without AI. PDFs and images are read by Claude."""
    extension = Path(filename).suffix.lower()
    try:
        if extension == ".csv":
            text = content.decode("utf-8-sig", errors="replace")
            rows = csv.reader(io.StringIO(text))
            lines = [" | ".join(cell.strip() for cell in row) for _, row in zip(range(500), rows)]
            return "\n".join(lines)[:MAX_EXTRACTED_TEXT]
        if extension == ".docx":
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
                paragraphs = _xml_text(archive.read("word/document.xml"), ns + "p", ns + "t")
            return "\n".join(paragraphs)[:MAX_EXTRACTED_TEXT]
        if extension == ".xlsx":
            ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                shared = []
                if "xl/sharedStrings.xml" in archive.namelist():
                    for item in ElementTree.fromstring(archive.read("xl/sharedStrings.xml")).iter(ns + "si"):
                        shared.append("".join(node.text or "" for node in item.iter(ns + "t")))
                sheets = sorted(name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name))
                lines = []
                for sheet in sheets[:3]:
                    lines.append(f"[{Path(sheet).stem}]")
                    for row in ElementTree.fromstring(archive.read(sheet)).iter(ns + "row"):
                        cells = []
                        for cell in row.iter(ns + "c"):
                            value = cell.find(ns + "v")
                            inline = cell.find(ns + "is")
                            if cell.get("t") == "s" and value is not None and value.text and value.text.isdigit():
                                index = int(value.text)
                                cells.append(shared[index] if index < len(shared) else "")
                            elif inline is not None:
                                cells.append("".join(node.text or "" for node in inline.iter(ns + "t")))
                            else:
                                cells.append(value.text if value is not None and value.text else "")
                        lines.append(" | ".join(cells))
                        if len(lines) > 600:
                            break
            return "\n".join(lines)[:MAX_EXTRACTED_TEXT]
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, ValueError):
        return ""
    return ""


def clean_extracted_fields(fields: object) -> dict:
    if not isinstance(fields, dict):
        raise ValueError("Extracted fields must be an object of field names and values.")
    if len(fields) > MAX_EXTRACTED_FIELDS:
        raise ValueError(f"Provide at most {MAX_EXTRACTED_FIELDS} extracted fields.")
    cleaned = {}
    for key, value in fields.items():
        name = str(key).strip()
        if not name or len(name) > 100:
            raise ValueError("Each extracted field needs a name of 100 characters or fewer.")
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        cleaned[name] = str(value).strip()[:1000]
    return cleaned


DEMO_EXTRACTIONS = {
    "Aanya Mehta": [
        (
            "6_month_statement.pdf",
            {"Account holder": "Aanya Mehta", "Bank": "Example Co-op Bank", "Period": "Apr - Sep", "Average monthly credit": "Rs 94,200", "Closing balance": "Rs 1,12,480", "Bounced cheques": "2"},
            ("warning", "2 cheque bounces in the statement", "Two returned cheques in July and August. Ask the applicant to explain."),
        ),
        ("PAN_card.pdf", {"Name": "Aanya Mehta", "PAN": "ABCDE1234F"}, None),
    ],
    "Arjun Rao": [
        (
            "credit_bureau_report.pdf",
            {"Name": "Arjun Rao", "Credit score": "718", "Active loans": "3", "Missed payments (12 months)": "1"},
            ("critical", "Name differs between documents", "The credit report says 'Arjun K Rao' but the Aadhaar says 'Arjun Rao'. Verify identity before proceeding."),
        ),
    ],
}


class Store:
    def __init__(self, database_path: Path, upload_dir: Path, seed: bool = True):
        self.database_path = Path(database_path)
        self.upload_dir = Path(upload_dir)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()
        if seed:
            self._seed_demo_data()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    id TEXT PRIMARY KEY,
                    applicant_name TEXT NOT NULL,
                    email TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    city TEXT NOT NULL DEFAULT '',
                    loan_type TEXT NOT NULL,
                    amount INTEGER NOT NULL CHECK (amount > 0),
                    purpose TEXT NOT NULL DEFAULT '',
                    monthly_income INTEGER,
                    credit_score INTEGER,
                    status TEXT NOT NULL DEFAULT 'New',
                    reviewer_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    application_id TEXT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    file_path TEXT,
                    mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                    size INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    content BLOB,
                    extracted_text TEXT NOT NULL DEFAULT '',
                    extracted_data TEXT NOT NULL DEFAULT '{}',
                    extracted_at TEXT
                );
                CREATE TABLE IF NOT EXISTS flags (
                    id TEXT PRIMARY KEY,
                    application_id TEXT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                    document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
                    severity TEXT NOT NULL DEFAULT 'warning',
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'claude',
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                """
            )
            existing = {row["name"] for row in connection.execute("PRAGMA table_info(documents)").fetchall()}
            for column, definition in (
                ("content", "BLOB"),
                ("extracted_text", "TEXT NOT NULL DEFAULT ''"),
                ("extracted_data", "TEXT NOT NULL DEFAULT '{}'"),
                ("extracted_at", "TEXT"),
            ):
                if column not in existing:
                    connection.execute(f"ALTER TABLE documents ADD COLUMN {column} {definition}")

    def _seed_demo_data(self) -> None:
        with self._connect() as connection:
            if connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0]:
                return

        examples = [
            {
                "applicant_name": "Aanya Mehta",
                "email": "aanya.mehta@example.org",
                "phone": "+91 90000 10001",
                "city": "Pune",
                "loan_type": "Business loan",
                "amount": 850000,
                "purpose": "Expand a neighborhood stationery shop",
                "monthly_income": 94000,
                "credit_score": 742,
                "status": "In review",
                "reviewer_note": "Bank statement received. Verify address proof.",
                "documents": [("PAN_card.pdf", "Identity"), ("6_month_statement.pdf", "Bank statement")],
            },
            {
                "applicant_name": "Arjun Rao",
                "email": "arjun.rao@example.org",
                "phone": "+91 90000 10002",
                "city": "Hyderabad",
                "loan_type": "Personal loan",
                "amount": 300000,
                "purpose": "Home renovation",
                "monthly_income": 78000,
                "credit_score": 718,
                "status": "Needs info",
                "reviewer_note": "Please request the latest salary slip.",
                "documents": [("Aadhaar.pdf", "Identity"), ("credit_bureau_report.pdf", "Credit report")],
            },
            {
                "applicant_name": "Nisha Kulkarni",
                "email": "nisha.kulkarni@example.org",
                "phone": "+91 90000 10003",
                "city": "Mumbai",
                "loan_type": "Personal loan",
                "amount": 475000,
                "purpose": "Education expenses",
                "monthly_income": 112000,
                "credit_score": 765,
                "status": "New",
                "reviewer_note": "",
                "documents": [("passport.pdf", "Identity")],
            },
            {
                "applicant_name": "Kabir Sharma",
                "email": "kabir.sharma@example.org",
                "phone": "+91 90000 10004",
                "city": "Jaipur",
                "loan_type": "Business loan",
                "amount": 1200000,
                "purpose": "Purchase equipment for a small bakery",
                "monthly_income": 156000,
                "credit_score": 733,
                "status": "Approved",
                "reviewer_note": "Documents checked by the review team.",
                "documents": [("PAN_card.pdf", "Identity"), ("bank_statement.pdf", "Bank statement")],
            },
        ]
        for example in examples:
            documents = example.pop("documents")
            application = self.create_application(example)
            registered = {}
            for name, category in documents:
                registered[name] = self.register_document(application["id"], name, category)
            demo = DEMO_EXTRACTIONS.get(example["applicant_name"])
            if demo:
                for name, fields, flag in demo:
                    document = registered[name]
                    self.save_extraction(document["id"], fields)
                    if flag:
                        self.add_flag(application["id"], flag[1], flag[2], flag[0], document["id"])

    @staticmethod
    def _validate_application(values: dict, partial: bool = False) -> dict:
        cleaned = {}
        for key, value in values.items():
            if key not in EDITABLE_FIELDS:
                continue
            if key in {"applicant_name", "loan_type", "purpose", "city", "email", "phone", "reviewer_note"}:
                value = str(value).strip()
                if key in {"applicant_name", "loan_type"} and not value:
                    raise ValueError(f"{key.replace('_', ' ').capitalize()} is required.")
                if len(value) > 500:
                    raise ValueError(f"{key.replace('_', ' ').capitalize()} must be 500 characters or fewer.")
            elif key in {"amount", "monthly_income", "credit_score"}:
                if value in ("", None):
                    value = None
                else:
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        raise ValueError(f"{key.replace('_', ' ').capitalize()} must be a whole number.") from None
                    if value < 0:
                        raise ValueError(f"{key.replace('_', ' ').capitalize()} cannot be negative.")
                if key == "amount" and (value is None or value == 0):
                    raise ValueError("Loan amount must be greater than zero.")
                if key == "credit_score" and value is not None and value > 900:
                    raise ValueError("Credit score must be 900 or lower.")
            elif key == "status":
                if value not in STATUSES:
                    raise ValueError("Choose a valid application status.")
            cleaned[key] = value
        return cleaned

    @staticmethod
    def _application_from_row(row: sqlite3.Row) -> dict:
        application = dict(row)
        application["document_count"] = application.pop("document_count", 0)
        return application

    def create_application(self, values: dict) -> dict:
        cleaned = self._validate_application(values)
        if not cleaned.get("applicant_name") or not cleaned.get("loan_type"):
            raise ValueError("Applicant name and loan type are required.")
        if "amount" not in cleaned:
            raise ValueError("Loan amount is required.")
        now = utc_now()
        application_id = f"LN-{uuid.uuid4().hex[:8].upper()}"
        fields = [
            "id",
            *cleaned.keys(),
            "created_at",
            "updated_at",
        ]
        values_to_insert = [application_id, *cleaned.values(), now, now]
        placeholders = ", ".join("?" for _ in fields)
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO applications ({', '.join(fields)}) VALUES ({placeholders})",
                values_to_insert,
            )
        return self.get_application(application_id)

    def update_application(self, application_id: str, values: dict) -> dict:
        cleaned = self._validate_application(values, partial=True)
        if not cleaned:
            raise ValueError("Provide at least one editable application field.")
        if not self._application_exists(application_id):
            raise KeyError("Application not found.")
        assignments = [f"{key} = ?" for key in cleaned]
        assignments.append("updated_at = ?")
        with self._connect() as connection:
            connection.execute(
                f"UPDATE applications SET {', '.join(assignments)} WHERE id = ?",
                [*cleaned.values(), utc_now(), application_id],
            )
        return self.get_application(application_id)

    def delete_application(self, application_id: str) -> None:
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM applications WHERE id = ?", (application_id,)
            ).fetchone() is None:
                raise KeyError("Application not found.")
            paths = [
                row["file_path"]
                for row in connection.execute(
                    "SELECT file_path FROM documents WHERE application_id = ? AND file_path IS NOT NULL",
                    (application_id,),
                ).fetchall()
            ]
            connection.execute("DELETE FROM applications WHERE id = ?", (application_id,))
        for path in paths:
            Path(path).unlink(missing_ok=True)

    def _application_exists(self, application_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM applications WHERE id = ?", (application_id,)
            ).fetchone() is not None

    def list_applications(self, search: str = "", status: str = "", limit: int = 100) -> list[dict]:
        if status and status not in STATUSES:
            raise ValueError("Choose a valid application status.")
        limit = max(1, min(int(limit), 100))
        clauses = []
        params = []
        if search.strip():
            term = f"%{search.strip()}%"
            clauses.append(
                "(a.applicant_name LIKE ? OR a.id LIKE ? OR a.city LIKE ? OR a.loan_type LIKE ?)"
            )
            params.extend([term, term, term, term])
        if status:
            clauses.append("a.status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT a.*, COUNT(d.id) AS document_count,
                       (SELECT COUNT(*) FROM flags f WHERE f.application_id = a.id AND f.resolved = 0) AS open_flags,
                       (SELECT COUNT(*) FROM flags f WHERE f.application_id = a.id AND f.resolved = 0 AND f.severity = 'critical') AS critical_flags
                FROM applications a
                LEFT JOIN documents d ON d.application_id = a.id
                {where}
                GROUP BY a.id
                ORDER BY a.updated_at DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return [self._application_from_row(row) for row in rows]

    def get_application(self, application_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT a.*, COUNT(d.id) AS document_count,
                       (SELECT COUNT(*) FROM flags f WHERE f.application_id = a.id AND f.resolved = 0) AS open_flags,
                       (SELECT COUNT(*) FROM flags f WHERE f.application_id = a.id AND f.resolved = 0 AND f.severity = 'critical') AS critical_flags
                FROM applications a
                LEFT JOIN documents d ON d.application_id = a.id
                WHERE a.id = ?
                GROUP BY a.id
                """,
                (application_id,),
            ).fetchone()
            if row is None:
                raise KeyError("Application not found.")
            application = self._application_from_row(row)
            application["documents"] = [
                self._document_summary(document)
                for document in connection.execute(
                    f"""
                    SELECT d.id, d.application_id, d.name, d.category, d.notes, d.mime_type, d.size, d.created_at,
                           {HAS_FILE_SQL} AS has_file, d.extracted_data, d.extracted_at,
                           length(d.extracted_text) > 0 AS has_text
                    FROM documents d WHERE d.application_id = ? ORDER BY d.created_at DESC
                    """,
                    (application_id,),
                ).fetchall()
            ]
            application["flags"] = self._flags(connection, "f.application_id = ?", (application_id,))
        return application

    @staticmethod
    def _document_summary(row: sqlite3.Row) -> dict:
        document = dict(row)
        try:
            document["extracted_data"] = json.loads(document.get("extracted_data") or "{}")
        except json.JSONDecodeError:
            document["extracted_data"] = {}
        for key in ("has_file", "has_text"):
            if key in document:
                document[key] = bool(document[key])
        return document

    @staticmethod
    def _flags(connection: sqlite3.Connection, where: str, params: tuple) -> list[dict]:
        rows = connection.execute(
            f"""
            SELECT f.id, f.application_id, f.document_id, f.severity, f.title, f.detail, f.source,
                   f.resolved, f.created_at, d.name AS document_name
            FROM flags f LEFT JOIN documents d ON d.id = f.document_id
            WHERE {where}
            ORDER BY f.resolved ASC,
                     CASE f.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                     f.created_at DESC
            """,
            params,
        ).fetchall()
        return [{**dict(row), "resolved": bool(row["resolved"])} for row in rows]

    def get_document(self, document_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT d.id, d.application_id, d.name, d.category, d.notes, d.mime_type, d.size, d.created_at,
                       {HAS_FILE_SQL} AS has_file, d.extracted_data, d.extracted_text, d.extracted_at,
                       a.applicant_name
                FROM documents d JOIN applications a ON a.id = d.application_id
                WHERE d.id = ?
                """,
                (document_id,),
            ).fetchone()
            if row is None:
                raise KeyError("Document not found.")
            document = self._document_summary(row)
            document["flags"] = self._flags(connection, "f.document_id = ?", (document_id,))
        return document

    def list_documents(self, search: str = "", category: str = "") -> dict:
        if category and category not in DOCUMENT_CATEGORIES:
            raise ValueError("Choose a valid document category.")
        clauses = []
        params = []
        if search.strip():
            term = f"%{search.strip()}%"
            clauses.append("(d.name LIKE ? OR a.applicant_name LIKE ? OR a.id LIKE ?)")
            params.extend([term, term, term])
        if category:
            clauses.append("d.category = ?")
            params.append(category)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT d.id, d.application_id, d.name, d.category, d.notes, d.mime_type, d.size,
                       d.created_at, {HAS_FILE_SQL} AS has_file,
                       d.extracted_at IS NOT NULL OR length(d.extracted_text) > 0 AS has_extraction,
                       (SELECT COUNT(*) FROM flags f WHERE f.document_id = d.id AND f.resolved = 0) AS open_flags,
                       a.applicant_name
                FROM documents d JOIN applications a ON a.id = d.application_id
                {where}
                ORDER BY d.created_at DESC
                LIMIT 200
                """,
                params,
            ).fetchall()
            counts = {
                row["category"]: row["count"]
                for row in connection.execute(
                    "SELECT category, COUNT(*) AS count FROM documents GROUP BY category"
                ).fetchall()
            }
        return {"documents": [dict(row) for row in rows], "category_counts": counts}

    def dashboard(self, search: str = "") -> dict:
        applications = self.list_applications(search=search)
        with self._connect() as connection:
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM applications GROUP BY status"
                ).fetchall()
            }
            total_documents = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            category_counts = {
                row["category"]: row["count"]
                for row in connection.execute(
                    "SELECT category, COUNT(*) AS count FROM documents GROUP BY category"
                ).fetchall()
            }
            queue_count = connection.execute(
                "SELECT COUNT(*) FROM applications WHERE status IN ('New', 'In review', 'Needs info')"
            ).fetchone()[0]
            flagged_cases = connection.execute(
                "SELECT COUNT(DISTINCT application_id) FROM flags WHERE resolved = 0"
            ).fetchone()[0]
            open_flags = connection.execute("SELECT COUNT(*) FROM flags WHERE resolved = 0").fetchone()[0]
        return {
            "applications": applications,
            "stats": {
                "flagged_cases": flagged_cases,
                "open_flags": open_flags,
                "total_applications": sum(counts.values()),
                "in_review": counts.get("In review", 0),
                "needs_info": counts.get("Needs info", 0),
                "approved": counts.get("Approved", 0),
                "queue": queue_count,
                "documents": total_documents,
                "declined": counts.get("Declined", 0),
                "new": counts.get("New", 0),
            },
            "category_counts": category_counts,
        }

    def register_document(
        self,
        application_id: str,
        name: str,
        category: str,
        notes: str = "",
        file_path: str | None = None,
        mime_type: str = "application/octet-stream",
        size: int = 0,
        content: bytes | None = None,
        extracted_text: str = "",
    ) -> dict:
        if not self._application_exists(application_id):
            raise KeyError("Application not found.")
        name = str(name).strip()
        if not name or len(name) > 255:
            raise ValueError("Document name must be between 1 and 255 characters.")
        if category not in DOCUMENT_CATEGORIES:
            raise ValueError("Choose a valid document category.")
        document_id = f"DC-{uuid.uuid4().hex[:10].upper()}"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO documents
                    (id, application_id, name, category, notes, file_path, mime_type, size, created_at,
                     content, extracted_text, extracted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    application_id,
                    name,
                    category,
                    str(notes)[:500],
                    file_path,
                    mime_type,
                    size,
                    utc_now(),
                    content,
                    extracted_text,
                    utc_now() if extracted_text else None,
                ),
            )
        return {
            "id": document_id,
            "application_id": application_id,
            "name": name,
            "category": category,
            "notes": str(notes)[:500],
            "mime_type": mime_type,
            "size": size,
            "has_file": content is not None or file_path is not None,
            "has_text": bool(extracted_text),
        }

    def update_document(self, document_id: str, values: dict) -> dict:
        allowed = {"category", "notes", "extracted_data"}
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            raise ValueError("Provide a document category, note or extracted data to update.")
        if "category" in updates and updates["category"] not in DOCUMENT_CATEGORIES:
            raise ValueError("Choose a valid document category.")
        if "notes" in updates:
            updates["notes"] = str(updates["notes"])[:500]
        if "extracted_data" in updates:
            updates["extracted_data"] = json.dumps(clean_extracted_fields(updates["extracted_data"]), ensure_ascii=False)
            updates["extracted_at"] = utc_now()
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM documents WHERE id = ?", (document_id,)).fetchone() is None:
                raise KeyError("Document not found.")
            connection.execute(
                f"UPDATE documents SET {', '.join(f'{key} = ?' for key in updates)} WHERE id = ?",
                [*updates.values(), document_id],
            )
        return self.get_document(document_id)

    def save_extraction(self, document_id: str, fields: dict | None = None, text: str | None = None) -> dict:
        updates: dict = {}
        if fields is not None:
            updates["extracted_data"] = json.dumps(clean_extracted_fields(fields), ensure_ascii=False)
        if text is not None:
            updates["extracted_text"] = str(text)[:MAX_EXTRACTED_TEXT]
        if not updates:
            raise ValueError("Provide extracted fields or extracted text to save.")
        updates["extracted_at"] = utc_now()
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM documents WHERE id = ?", (document_id,)).fetchone() is None:
                raise KeyError("Document not found.")
            connection.execute(
                f"UPDATE documents SET {', '.join(f'{key} = ?' for key in updates)} WHERE id = ?",
                [*updates.values(), document_id],
            )
        return self.get_document(document_id)

    def add_flag(
        self,
        application_id: str,
        title: str,
        detail: str = "",
        severity: str = "warning",
        document_id: str | None = None,
        source: str = "claude",
    ) -> dict:
        if not self._application_exists(application_id):
            raise KeyError("Application not found.")
        title = str(title).strip()
        if not title or len(title) > 200:
            raise ValueError("A flag needs a title of 200 characters or fewer.")
        if severity not in FLAG_SEVERITIES:
            raise ValueError("Flag severity must be info, warning or critical.")
        detail = str(detail or "").strip()[:1000]
        flag_id = f"FL-{uuid.uuid4().hex[:8].upper()}"
        with self._connect() as connection:
            if document_id:
                owner = connection.execute(
                    "SELECT application_id FROM documents WHERE id = ?", (document_id,)
                ).fetchone()
                if owner is None or owner["application_id"] != application_id:
                    raise KeyError("Document not found on this application.")
            connection.execute(
                """
                INSERT INTO flags (id, application_id, document_id, severity, title, detail, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (flag_id, application_id, document_id or None, severity, title, detail, source, utc_now()),
            )
            connection.execute("UPDATE applications SET updated_at = ? WHERE id = ?", (utc_now(), application_id))
            return self._flags(connection, "f.id = ?", (flag_id,))[0]

    def update_flag(self, flag_id: str, values: dict) -> dict:
        updates: dict = {}
        if "resolved" in values:
            updates["resolved"] = 1 if values["resolved"] in (True, 1, "true") else 0
        if "severity" in values:
            if values["severity"] not in FLAG_SEVERITIES:
                raise ValueError("Flag severity must be info, warning or critical.")
            updates["severity"] = values["severity"]
        if "title" in values:
            title = str(values["title"]).strip()
            if not title or len(title) > 200:
                raise ValueError("A flag needs a title of 200 characters or fewer.")
            updates["title"] = title
        if "detail" in values:
            updates["detail"] = str(values["detail"]).strip()[:1000]
        if not updates:
            raise ValueError("Provide a flag field to update.")
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM flags WHERE id = ?", (flag_id,)).fetchone() is None:
                raise KeyError("Flag not found.")
            connection.execute(
                f"UPDATE flags SET {', '.join(f'{key} = ?' for key in updates)} WHERE id = ?",
                [*updates.values(), flag_id],
            )
            return self._flags(connection, "f.id = ?", (flag_id,))[0]

    def delete_flag(self, flag_id: str) -> None:
        with self._connect() as connection:
            if connection.execute("DELETE FROM flags WHERE id = ?", (flag_id,)).rowcount == 0:
                raise KeyError("Flag not found.")

    def save_upload(self, application_id: str, filename: str, category: str, content: bytes) -> dict:
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError("Each document must be 15 MB or smaller.")
        if not content:
            raise ValueError("The file is empty.")
        safe_name = Path(filename.replace("\\", "/")).name.strip()
        extension = Path(safe_name).suffix.lower()
        if not safe_name or len(safe_name) > 255:
            raise ValueError("Document name must be between 1 and 255 characters.")
        if extension not in ALLOWED_EXTENSIONS:
            raise ValueError("This file type is not supported. Upload PDF, image, DOCX, XLSX or CSV files.")
        mime_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        return self.register_document(
            application_id,
            safe_name,
            category,
            mime_type=mime_type,
            size=len(content),
            content=content,
            extracted_text=extract_text(safe_name, content),
        )

    def document_file(self, document_id: str) -> tuple[dict, bytes]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT name, file_path, mime_type, content FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
        if row is None:
            raise KeyError("Document not found.")
        details = {"name": row["name"], "mime_type": row["mime_type"]}
        if row["content"] is not None:
            return details, bytes(row["content"])
        if not row["file_path"]:
            raise ValueError("This document is a record only; no file was uploaded.")
        path = Path(row["file_path"]).resolve()
        if path.parent != self.upload_dir.resolve() or not path.is_file():
            raise KeyError("Document file not found.")
        return details, path.read_bytes()


def tool_definitions() -> list[dict]:
    return [
        {
            "name": "create_loan_application",
            "description": "Create a loan application in the underwriting desk. The application is saved for the review team; this does not approve or score the loan.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "applicant_name": {"type": "string", "description": "Applicant's full name"},
                    "loan_type": {"type": "string", "description": "For example, Personal loan or Business loan"},
                    "amount": {"type": "integer", "minimum": 1, "description": "Requested amount in INR"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "city": {"type": "string"},
                    "purpose": {"type": "string"},
                    "monthly_income": {"type": "integer", "minimum": 0},
                    "credit_score": {"type": "integer", "minimum": 0, "maximum": 900},
                    "reviewer_note": {"type": "string"},
                },
                "required": ["applicant_name", "loan_type", "amount"],
            },
        },
        {
            "name": "list_loan_applications",
            "description": "Find saved loan applications, optionally filtered by status or applicant name, city, loan type, or case ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "status": {"type": "string", "enum": sorted(STATUSES)},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
        },
        {
            "name": "get_loan_application",
            "description": "Read an application and its organized document list using its case ID.",
            "inputSchema": {
                "type": "object",
                "properties": {"application_id": {"type": "string"}},
                "required": ["application_id"],
            },
        },
        {
            "name": "update_loan_application",
            "description": "Edit application details, reviewer notes, or the manual review status.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "application_id": {"type": "string"},
                    "applicant_name": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "city": {"type": "string"},
                    "loan_type": {"type": "string"},
                    "amount": {"type": "integer", "minimum": 1},
                    "purpose": {"type": "string"},
                    "monthly_income": {"type": ["integer", "null"], "minimum": 0},
                    "credit_score": {"type": ["integer", "null"], "minimum": 0, "maximum": 900},
                    "status": {"type": "string", "enum": sorted(STATUSES)},
                    "reviewer_note": {"type": "string"},
                },
                "required": ["application_id"],
            },
        },
        {
            "name": "register_document",
            "description": "Add a document record to an application's file checklist. Use the web app to upload the actual file.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "application_id": {"type": "string"},
                    "name": {"type": "string", "description": "Document filename or description"},
                    "category": {"type": "string", "enum": sorted(DOCUMENT_CATEGORIES)},
                    "notes": {"type": "string"},
                },
                "required": ["application_id", "name", "category"],
            },
        },
        {
            "name": "upload_document",
            "description": "Store a file on an application. Send the file bytes as base64 (up to 8 MB). Use this when you hold the file contents; otherwise a reviewer can upload in the web app.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "application_id": {"type": "string"},
                    "filename": {"type": "string", "description": "Including the extension: pdf, png, jpg, jpeg, webp, docx, xlsx or csv"},
                    "category": {"type": "string", "enum": sorted(DOCUMENT_CATEGORIES)},
                    "content_base64": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["application_id", "filename", "category", "content_base64"],
            },
        },
        {
            "name": "read_document",
            "description": "Open a stored document so you can read it. Returns its details, any saved extracted text and fields, and the file itself (images and PDFs are included for you to read). Use this before save_document_extraction.",
            "inputSchema": {
                "type": "object",
                "properties": {"document_id": {"type": "string"}},
                "required": ["document_id"],
            },
        },
        {
            "name": "save_document_extraction",
            "description": "Save what you read from a document: a set of field name/value pairs (for example Name, PAN, Average monthly credit) and optionally the full text. Reviewers see this next to the file and can edit it.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "fields": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}},
                    "text": {"type": "string", "description": "Optional transcription or summary of the document"},
                },
                "required": ["document_id"],
            },
        },
        {
            "name": "flag_application",
            "description": "Raise a flag for a human reviewer, for example a name mismatch, a possible tampered document or a missing page. You flag; people decide.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "application_id": {"type": "string"},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "severity": {"type": "string", "enum": sorted(FLAG_SEVERITIES)},
                    "document_id": {"type": "string", "description": "Optional: the document the flag is about"},
                },
                "required": ["application_id", "title"],
            },
        },
        {
            "name": "resolve_flag",
            "description": "Mark a flag as resolved, or reopen it.",
            "inputSchema": {
                "type": "object",
                "properties": {"flag_id": {"type": "string"}, "resolved": {"type": "boolean"}},
                "required": ["flag_id"],
            },
        },
    ]


def call_tool(store: Store, name: str, arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a JSON object.")
    if name == "create_loan_application":
        return store.create_application(arguments)
    if name == "list_loan_applications":
        return {
            "applications": store.list_applications(
                search=str(arguments.get("search", "")),
                status=str(arguments.get("status", "")),
                limit=arguments.get("limit", 100),
            )
        }
    if name == "get_loan_application":
        return store.get_application(str(arguments.get("application_id", "")))
    if name == "update_loan_application":
        application_id = str(arguments.get("application_id", ""))
        changes = {key: value for key, value in arguments.items() if key != "application_id"}
        return store.update_application(application_id, changes)
    if name == "register_document":
        return store.register_document(
            str(arguments.get("application_id", "")),
            arguments.get("name", ""),
            arguments.get("category", ""),
            arguments.get("notes", ""),
        )
    if name == "upload_document":
        try:
            content = base64.b64decode(str(arguments.get("content_base64", "")), validate=False)
        except (binascii.Error, ValueError):
            raise ValueError("content_base64 is not valid base64.") from None
        if len(content) > MAX_MCP_FILE_BYTES:
            raise ValueError("Files sent through Claude must be 8 MB or smaller; upload larger files in the web app.")
        document = store.save_upload(
            str(arguments.get("application_id", "")),
            str(arguments.get("filename", "")),
            str(arguments.get("category", "")),
            content,
        )
        if arguments.get("notes"):
            store.update_document(document["id"], {"notes": arguments["notes"]})
        return document
    if name == "read_document":
        document = store.get_document(str(arguments.get("document_id", "")))
        text = {key: document[key] for key in (
            "id", "application_id", "applicant_name", "name", "category", "mime_type", "size",
            "extracted_text", "extracted_data", "flags",
        )}
        content = [{"type": "text", "text": json.dumps(text, ensure_ascii=False)}]
        if document["has_file"] and document["mime_type"] in INLINE_TYPES:
            _, data = store.document_file(document["id"])
            if len(data) > MAX_MCP_FILE_BYTES:
                content.append({"type": "text", "text": "The file is larger than 8 MB, so it was not attached."})
            elif document["mime_type"] in IMAGE_TYPES:
                content.append({"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": document["mime_type"]})
            else:
                content.append({
                    "type": "resource",
                    "resource": {
                        "uri": f"folio://documents/{document['id']}",
                        "mimeType": document["mime_type"],
                        "blob": base64.b64encode(data).decode("ascii"),
                    },
                })
        elif not document["has_file"]:
            content.append({"type": "text", "text": "This document is a record only; no file has been uploaded yet."})
        return {"_mcp_content": content}
    if name == "save_document_extraction":
        return store.save_extraction(
            str(arguments.get("document_id", "")), arguments.get("fields"), arguments.get("text")
        )
    if name == "flag_application":
        return store.add_flag(
            str(arguments.get("application_id", "")),
            arguments.get("title", ""),
            arguments.get("detail", ""),
            arguments.get("severity", "warning"),
            arguments.get("document_id") or None,
        )
    if name == "resolve_flag":
        return store.update_flag(
            str(arguments.get("flag_id", "")), {"resolved": arguments.get("resolved", True)}
        )
    raise ValueError(f"Unknown tool: {name}")


def mcp_response(message: dict, store: Store) -> tuple[int, dict | None]:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid JSON-RPC request."}}
    request_id = message.get("id")
    method = message.get("method")
    if not isinstance(method, str):
        return 400, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "Method is required."}}
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return 400, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Parameters must be a JSON object."}}
    if method.startswith("notifications/") and request_id is None:
        return 202, None
    if method == "initialize":
        requested_version = params.get("protocolVersion")
        protocol_version = (
            requested_version
            if requested_version == MCP_PROTOCOL_VERSION
            else MCP_PROTOCOL_VERSION
        )
        return 200, {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "loan-desk", "version": "1.0.0"},
                "instructions": "Use the tools to organize and edit applicant case records. To process a document: read_document, then save_document_extraction with the fields you read, then flag_application for anything a reviewer should check (mismatched names, unclear or altered documents, missing pages). Flag, never decide: loan review decisions are always manual.",
            },
        }
    if method == "ping":
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tool_definitions()}}
    if method == "tools/call":
        try:
            result = call_tool(store, params.get("name", ""), params.get("arguments", {}))
            content = result.pop("_mcp_content", None) or [
                {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
            ]
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": content,
                    "isError": False,
                },
            }
        except (ValueError, KeyError) as error:
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": str(error)}],
                    "isError": True,
                },
            }
    return 200, {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


DEFAULT_STORE: Store | None = None


def get_default_store() -> Store:
    global DEFAULT_STORE
    if DEFAULT_STORE is None:
        DEFAULT_STORE = Store(DATABASE_PATH, UPLOAD_DIR)
    return DEFAULT_STORE


class LoanDeskHandler(BaseHTTPRequestHandler):
    server_version = "LoanDesk/1.0"
    store: Store

    def __init__(self, request, client_address, server):
        self.store = getattr(server, "store", None) or get_default_store()
        super().__init__(request, client_address, server)

    def log_message(self, format_string: str, *args) -> None:
        print(f"{self.address_string()} - {format_string % args}")

    def _send(self, status: int, body: bytes, content_type: str, extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _cookie_token(self) -> str | None:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value
        return None

    def _current_user(self) -> str | None:
        return session_user(self._cookie_token())

    def _is_https(self) -> bool:
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _session_cookie(self, token: str, max_age: int) -> str:
        secure = "; Secure" if self._is_https() else ""
        return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"

    def _mcp_authorized(self, path: str) -> bool:
        expected = getattr(self.server, "mcp_token", None) or get_mcp_token()
        supplied = ""
        if path.startswith("/mcp/"):
            supplied = path[len("/mcp/"):].strip("/")
        elif parse_qs(urlsplit(self.path).query).get("token"):
            supplied = parse_qs(urlsplit(self.path).query)["token"][0]
        else:
            header = self.headers.get("Authorization", "")
            if header.lower().startswith("bearer "):
                supplied = header[7:].strip()
        return bool(supplied) and hmac.compare_digest(supplied, expected)

    def _handle_login(self) -> None:
        ip = self.client_address[0]
        with AUTH_LOCK:
            count, until = FAILED_LOGINS.get(ip, (0, 0.0))
            if until < time.time():
                count = 0
            locked = count >= 5
        if locked:
            self._send_json(429, {"error": "Too many failed attempts. Try again in a minute."})
            return
        body = self._read_json()
        username, password = credentials()
        supplied_user = str(body.get("username", ""))
        supplied_password = str(body.get("password", ""))
        valid = hmac.compare_digest(supplied_user.encode(), username.encode()) & hmac.compare_digest(
            supplied_password.encode(), password.encode()
        )
        if not valid:
            with AUTH_LOCK:
                FAILED_LOGINS[ip] = (count + 1, time.time() + 60)
            time.sleep(0.4)
            self._send_json(401, {"error": "Incorrect username or password."})
            return
        with AUTH_LOCK:
            FAILED_LOGINS.pop(ip, None)
        token = create_session(username)
        body_bytes = json.dumps({"username": username}).encode("utf-8")
        self._send(
            200,
            body_bytes,
            "application/json; charset=utf-8",
            {"Set-Cookie": self._session_cookie(token, SESSION_TTL_SECONDS)},
        )

    def _send_json(self, status: int, value: dict | list | None) -> None:
        body = b"" if value is None else json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _read_json(self, limit: int = 1024 * 1024) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > limit:
            raise ValueError(f"Request body must be {limit // (1024 * 1024)} MB or smaller.")
        body = self.rfile.read(length)
        try:
            value = json.loads(body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Request body must be valid JSON.") from None
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object.")
        return value

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            user = self._current_user()
            if path.startswith("/api/") and path != "/api/health" and not user:
                self._send_json(401, {"error": "Please sign in."})
            elif path == "/api/health":
                self._send_json(200, {"status": "ok"})
            elif path == "/api/me":
                self._send_json(200, {"username": user})
            elif path == "/api/mcp-info":
                scheme = "https" if self._is_https() else "http"
                host = self.headers.get("Host", "localhost")
                self._send_json(200, {"url": f"{scheme}://{host}/mcp/{get_mcp_token()}"})
            elif path == "/mcp" or path.startswith("/mcp/"):
                self._send(
                    405,
                    json.dumps({"error": "Use POST for MCP requests."}).encode("utf-8"),
                    "application/json; charset=utf-8",
                    {"Allow": "POST"},
                )
            elif path == "/api/documents":
                self._send_json(
                    200,
                    self.store.list_documents(
                        query.get("search", [""])[0], query.get("category", [""])[0]
                    ),
                )
            elif path == "/api/dashboard":
                self._send_json(200, self.store.dashboard(query.get("search", [""])[0]))
            elif re.fullmatch(r"/api/loans/[^/]+", path):
                self._send_json(200, self.store.get_application(path.rsplit("/", 1)[1]))
            elif re.fullmatch(r"/api/documents/[^/]+/(download|view)", path):
                document_id, action = path.split("/")[-2:]
                details, data = self.store.document_file(document_id)
                filename = re.sub(r'[^A-Za-z0-9._ -]', "_", details["name"])
                inline = action == "view" and details["mime_type"] in INLINE_TYPES
                self._send(
                    200,
                    data,
                    details["mime_type"],
                    {"Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{filename}"'},
                )
            elif re.fullmatch(r"/api/documents/[^/]+", path):
                self._send_json(200, self.store.get_document(path.rsplit("/", 1)[1]))
            elif path == "/":
                page = "index.html" if user else "login.html"
                self._serve_file(ROOT / "static" / page, "text/html; charset=utf-8")
            elif path in {"/static/app.js", "/static/login.js", "/static/styles.css"}:
                file_path = ROOT / path.lstrip("/")
                content_type = "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
                self._serve_file(file_path, content_type)
            else:
                self._send_json(404, {"error": "Not found."})
        except KeyError as error:
            self._send_json(404, {"error": str(error).strip("'")})
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
        except (OSError, sqlite3.Error) as error:
            print(f"Request failed: {error}")
            self._send_json(500, {"error": "The request could not be completed."})

    def _serve_file(self, file_path: Path, content_type: str) -> None:
        if not file_path.is_file():
            self._send_json(404, {"error": "Page not found."})
            return
        self._send(200, file_path.read_bytes(), content_type)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        try:
            is_mcp = path == "/mcp" or path.startswith("/mcp/")
            if path == "/api/login":
                self._handle_login()
                return
            if path == "/api/logout":
                with AUTH_LOCK:
                    SESSIONS.pop(self._cookie_token() or "", None)
                self._send(
                    200,
                    b"{}",
                    "application/json; charset=utf-8",
                    {"Set-Cookie": self._session_cookie("", 0)},
                )
                return
            if is_mcp:
                if not self._mcp_authorized(path):
                    self._send_json(401, {"error": "Invalid or missing MCP access token."})
                    return
            elif not self._current_user():
                self._send_json(401, {"error": "Please sign in."})
                return
            if is_mcp:
                message = self._read_json(limit=12 * 1024 * 1024)
                status, response = mcp_response(message, self.store)
                self._send_json(status, response)
            elif path == "/api/loans":
                application = self.store.create_application(self._read_json())
                self._send_json(201, application)
            elif re.fullmatch(r"/api/loans/[^/]+/documents", path):
                application_id = path.split("/")[-2]
                documents = self._read_multipart_uploads(application_id)
                self._send_json(201, {"documents": documents})
            elif re.fullmatch(r"/api/documents/[^/]+", path):
                document_id = path.rsplit("/", 1)[1]
                self._send_json(200, self.store.update_document(document_id, self._read_json()))
            elif re.fullmatch(r"/api/loans/[^/]+/flags", path):
                body = self._read_json()
                flag = self.store.add_flag(
                    path.split("/")[-2], body.get("title", ""), body.get("detail", ""),
                    body.get("severity", "warning"), body.get("document_id") or None, source="reviewer",
                )
                self._send_json(201, flag)
            else:
                self._send_json(404, {"error": "Not found."})
        except KeyError as error:
            self._send_json(404, {"error": str(error).strip("'")})
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
        except (OSError, sqlite3.Error) as error:
            print(f"Request failed: {error}")
            self._send_json(500, {"error": "The request could not be completed."})

    def do_DELETE(self) -> None:
        path = unquote(urlsplit(self.path).path)
        try:
            if not self._current_user():
                self._send_json(401, {"error": "Please sign in."})
            elif re.fullmatch(r"/api/loans/[^/]+", path):
                self.store.delete_application(path.rsplit("/", 1)[1])
                self._send_json(200, {"deleted": True})
            elif re.fullmatch(r"/api/flags/[^/]+", path):
                self.store.delete_flag(path.rsplit("/", 1)[1])
                self._send_json(200, {"deleted": True})
            else:
                self._send_json(404, {"error": "Not found."})
        except KeyError as error:
            self._send_json(404, {"error": str(error).strip("'")})
        except (OSError, sqlite3.Error) as error:
            print(f"Request failed: {error}")
            self._send_json(500, {"error": "The request could not be completed."})

    def do_PATCH(self) -> None:
        path = unquote(urlsplit(self.path).path)
        try:
            if not self._current_user():
                self._send_json(401, {"error": "Please sign in."})
            elif re.fullmatch(r"/api/loans/[^/]+", path):
                application_id = path.rsplit("/", 1)[1]
                self._send_json(200, self.store.update_application(application_id, self._read_json()))
            elif re.fullmatch(r"/api/flags/[^/]+", path):
                self._send_json(200, self.store.update_flag(path.rsplit("/", 1)[1], self._read_json()))
            else:
                self._send_json(404, {"error": "Not found."})
        except KeyError as error:
            self._send_json(404, {"error": str(error).strip("'")})
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
        except (OSError, sqlite3.Error) as error:
            print(f"Request failed: {error}")
            self._send_json(500, {"error": "The request could not be completed."})

    def _read_multipart_uploads(self, application_id: str) -> list[dict]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data;"):
            raise ValueError("Upload must use multipart form data.")
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_UPLOAD_BYTES + 1024 * 1024:
            raise ValueError("Upload request is too large.")
        raw = self.rfile.read(length)
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + raw
        )
        category = ""
        files = []
        for part in message.iter_parts():
            field_name = part.get_param("name", header="content-disposition")
            if field_name == "category":
                category = part.get_content().strip()
            elif field_name == "files":
                filename = part.get_filename()
                if filename:
                    files.append((filename, part.get_payload(decode=True) or b""))
        if not files:
            raise ValueError("Choose at least one document to upload.")
        if len(files) > 20:
            raise ValueError("Upload up to 20 documents at a time.")
        if sum(len(content) for _, content in files) > MAX_UPLOAD_BYTES:
            raise ValueError("Combined document size must be 15 MB or smaller.")
        if category not in DOCUMENT_CATEGORIES:
            raise ValueError("Choose a valid document category.")
        for filename, _ in files:
            safe_name = Path(filename.replace("\\", "/")).name.strip()
            if not safe_name or len(safe_name) > 255:
                raise ValueError("Document name must be between 1 and 255 characters.")
            if Path(safe_name).suffix.lower() not in ALLOWED_EXTENSIONS:
                raise ValueError("This file type is not supported. Upload PDF, image, DOCX, XLSX or CSV files.")
        return [
            self.store.save_upload(application_id, filename, category, content)
            for filename, content in files
        ]

    def do_OPTIONS(self) -> None:
        self._send(
            204,
            b"",
            "text/plain",
            {
                "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, MCP-Protocol-Version",
            },
        )


def main() -> None:
    host = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), LoanDeskHandler)
    server.store = Store(DATABASE_PATH, UPLOAD_DIR)
    print(f"Loan Desk running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Loan Desk.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
