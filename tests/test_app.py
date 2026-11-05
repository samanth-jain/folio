import json
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import LoanDeskHandler, Store, call_tool, extract_text, mcp_response


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.store = Store(root / "test.sqlite3", root / "uploads", seed=False)

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_application(self):
        return self.store.create_application(
            {
                "applicant_name": "Priya Shah",
                "loan_type": "Personal loan",
                "amount": 400000,
                "city": "Pune",
            }
        )

    def test_create_update_and_read_application(self):
        application = self.create_application()
        updated = self.store.update_application(
            application["id"],
            {"status": "In review", "reviewer_note": "Identity check pending"},
        )
        self.assertEqual(updated["status"], "In review")
        self.assertEqual(updated["reviewer_note"], "Identity check pending")
        self.assertEqual(self.store.get_application(application["id"])["document_count"], 0)

    def test_invalid_application_status_is_rejected(self):
        application = self.create_application()
        with self.assertRaisesRegex(ValueError, "valid application status"):
            self.store.update_application(application["id"], {"status": "Auto-approved"})

    def test_document_registration_and_search(self):
        application = self.create_application()
        document = self.store.register_document(
            application["id"], "PAN card.pdf", "Identity", "Name legible"
        )
        detail = self.store.get_application(application["id"])
        self.assertEqual(detail["documents"][0]["id"], document["id"])
        self.assertFalse(detail["documents"][0]["has_file"])
        self.assertEqual(len(self.store.list_applications(search="Priya")), 1)

    def test_delete_application_removes_documents_and_files(self):
        application = self.create_application()
        document = self.store.save_upload(application["id"], "a.pdf", "Identity", b"%PDF-1.4 data")
        _, data = self.store.document_file(document["id"])
        self.assertEqual(data, b"%PDF-1.4 data")
        self.store.add_flag(application["id"], "Check", document_id=document["id"])
        self.store.delete_application(application["id"])
        self.assertEqual(self.store.list_applications(), [])
        with self.assertRaises(KeyError):
            self.store.document_file(document["id"])
        with self.assertRaises(KeyError):
            self.store.delete_application(application["id"])

    def test_uploaded_files_live_in_the_database(self):
        application = self.create_application()
        document = self.store.save_upload(application["id"], "income.csv", "Income", b"month,salary\nApr,80000\n")
        self.assertTrue(document["has_file"])
        self.assertEqual(list(self.store.upload_dir.iterdir()), [])
        reopened = Store(self.store.database_path, self.store.upload_dir, seed=False)
        _, data = reopened.document_file(document["id"])
        self.assertIn(b"80000", data)
        detail = reopened.get_document(document["id"])
        self.assertIn("Apr | 80000", detail["extracted_text"])

    def test_docx_and_xlsx_text_is_extracted(self):
        import io
        import zipfile

        docx = io.BytesIO()
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>Salary slip</w:t></w:r></w:p></w:body></w:document>",
            )
        self.assertEqual(extract_text("slip.docx", docx.getvalue()), "Salary slip")
        xlsx = io.BytesIO()
        with zipfile.ZipFile(xlsx, "w") as archive:
            archive.writestr(
                "xl/sharedStrings.xml",
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Name</t></si></sst>',
            )
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                '<row><c t="s"><v>0</v></c><c><v>42</v></c></row></sheetData></worksheet>',
            )
        self.assertIn("Name | 42", extract_text("a.xlsx", xlsx.getvalue()))
        self.assertEqual(extract_text("broken.docx", b"not a zip"), "")

    def test_extraction_is_saved_and_editable(self):
        application = self.create_application()
        document = self.store.register_document(application["id"], "PAN.pdf", "Identity")
        saved = self.store.save_extraction(document["id"], {"Name": "Priya Shah", "PAN": "ABCDE1234F", "Empty": None})
        self.assertEqual(saved["extracted_data"], {"Name": "Priya Shah", "PAN": "ABCDE1234F"})
        edited = self.store.update_document(document["id"], {"extracted_data": {"Name": "Priya S. Shah"}})
        self.assertEqual(edited["extracted_data"], {"Name": "Priya S. Shah"})
        listed = self.store.get_application(application["id"])["documents"][0]
        self.assertEqual(listed["extracted_data"]["Name"], "Priya S. Shah")
        with self.assertRaises(ValueError):
            self.store.save_extraction(document["id"], ["not", "an", "object"])

    def test_flags_are_counted_and_can_be_resolved(self):
        application = self.create_application()
        document = self.store.register_document(application["id"], "PAN.pdf", "Identity")
        warning = self.store.add_flag(application["id"], "Blurry scan", severity="warning", document_id=document["id"])
        self.store.add_flag(application["id"], "Name mismatch", "PAN says P Shah", "critical")
        listed = self.store.list_applications()[0]
        self.assertEqual((listed["open_flags"], listed["critical_flags"]), (2, 1))
        self.assertEqual(self.store.dashboard()["stats"]["flagged_cases"], 1)
        detail = self.store.get_application(application["id"])
        self.assertEqual(detail["flags"][0]["severity"], "critical")
        self.store.update_flag(warning["id"], {"resolved": True})
        self.assertEqual(self.store.list_applications()[0]["open_flags"], 1)
        self.assertEqual(self.store.list_documents()["documents"][0]["open_flags"], 0)
        with self.assertRaises(ValueError):
            self.store.add_flag(application["id"], "Bad", severity="urgent")
        with self.assertRaises(KeyError):
            self.store.add_flag(application["id"], "Wrong doc", document_id="DC-NOPE")
        self.store.delete_flag(warning["id"])
        with self.assertRaises(KeyError):
            self.store.delete_flag(warning["id"])

    def test_mcp_document_tools_read_extract_and_flag(self):
        import base64

        application = self.create_application()
        payload = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
        document = call_tool(self.store, "upload_document", {
            "application_id": application["id"], "filename": "aadhaar.png",
            "category": "Identity", "content_base64": payload,
        })
        status, response = mcp_response(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "read_document", "arguments": {"document_id": document["id"]}}},
            self.store,
        )
        content = response["result"]["content"]
        self.assertEqual([block["type"] for block in content], ["text", "image"])
        self.assertEqual(content[1]["mimeType"], "image/png")
        call_tool(self.store, "save_document_extraction", {
            "document_id": document["id"], "fields": {"Name": "Priya Shah", "Year of birth": 1991},
        })
        flag = call_tool(self.store, "flag_application", {
            "application_id": application["id"], "title": "Photo looks edited",
            "severity": "critical", "document_id": document["id"],
        })
        self.assertEqual(self.store.get_document(document["id"])["extracted_data"]["Year of birth"], "1991")
        self.assertEqual(self.store.get_document(document["id"])["flags"][0]["id"], flag["id"])
        self.assertTrue(call_tool(self.store, "resolve_flag", {"flag_id": flag["id"]})["resolved"])
        with self.assertRaises(ValueError):
            call_tool(self.store, "upload_document", {
                "application_id": application["id"], "filename": "x.exe",
                "category": "Other", "content_base64": payload,
            })

    def test_list_documents_filters_by_category_and_search(self):
        application = self.create_application()
        self.store.register_document(application["id"], "PAN.pdf", "Identity")
        self.store.register_document(application["id"], "stmt.pdf", "Bank statement")
        everything = self.store.list_documents()
        self.assertEqual(len(everything["documents"]), 2)
        self.assertEqual(everything["category_counts"]["Identity"], 1)
        self.assertEqual(self.store.list_documents(category="Identity")["documents"][0]["name"], "PAN.pdf")
        self.assertEqual(self.store.list_documents(search="Priya")["documents"][0]["applicant_name"], "Priya Shah")
        with self.assertRaises(ValueError):
            self.store.list_documents(category="Nope")

    def test_mcp_create_tool_persists_application(self):
        result = call_tool(
            self.store,
            "create_loan_application",
            {"applicant_name": "Priya Shah", "loan_type": "Personal loan", "amount": 400000},
        )
        self.assertEqual(self.store.get_application(result["id"])["applicant_name"], "Priya Shah")

    def test_mcp_initialization_and_tools_list(self):
        status, response = mcp_response(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            },
            self.store,
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["result"]["serverInfo"]["name"], "loan-desk")
        status, response = mcp_response(
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "initialize",
                "params": {"protocolVersion": "future-version"},
            },
            self.store,
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["result"]["protocolVersion"], "2025-03-26")
        status, response = mcp_response(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            self.store,
        )
        self.assertEqual(status, 200)
        self.assertIn("create_loan_application", [tool["name"] for tool in response["result"]["tools"]])

    def test_mcp_tool_error_is_reported_as_tool_error(self):
        status, response = mcp_response(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_loan_application", "arguments": {"application_id": "missing"}},
            },
            self.store,
        )
        self.assertEqual(status, 200)
        self.assertTrue(response["result"]["isError"])


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.store = Store(root / "http.sqlite3", root / "uploads", seed=False)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LoanDeskHandler)
        self.server.store = self.store
        self.server.mcp_token = "test-token"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.cookie = None

    def login(self):
        request = Request(
            self.base_url + "/api/login",
            data=json.dumps({"username": "admin", "password": "folio-demo"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            self.cookie = response.headers["Set-Cookie"].split(";")[0]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temp_dir.cleanup()

    def request(self, path, method="GET", body=None, headers=None):
        headers = dict(headers or {})
        if self.cookie:
            headers["Cookie"] = self.cookie
        request = Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request) as response:
                body = response.read()
                return response.status, json.loads(body) if body else None
        except HTTPError as error:
            body = error.read()
            return error.code, json.loads(body) if body else None

    def test_api_requires_login(self):
        status, _ = self.request("/api/dashboard")
        self.assertEqual(status, 401)
        status, _ = self.request(
            "/api/loans", "POST", b"{}", {"Content-Type": "application/json"}
        )
        self.assertEqual(status, 401)

    def test_wrong_password_rejected(self):
        status, _ = self.request(
            "/api/login",
            "POST",
            json.dumps({"username": "admin", "password": "nope"}).encode(),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 401)

    def test_api_create_edit_and_read(self):
        self.login()
        status, created = self.request(
            "/api/loans",
            "POST",
            json.dumps(
                {"applicant_name": "Priya Shah", "loan_type": "Personal loan", "amount": 400000}
            ).encode(),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 201)
        status, updated = self.request(
            f"/api/loans/{created['id']}",
            "PATCH",
            json.dumps({"status": "In review"}).encode(),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["status"], "In review")
        status, detail = self.request(f"/api/loans/{created['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["applicant_name"], "Priya Shah")
        status, _ = self.request(f"/api/loans/{created['id']}", "DELETE")
        self.assertEqual(status, 200)
        status, _ = self.request(f"/api/loans/{created['id']}")
        self.assertEqual(status, 404)

    def test_mcp_http_tool_call_creates_saved_case(self):
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "create_loan_application",
                    "arguments": {
                        "applicant_name": "MCP Applicant",
                        "loan_type": "Business loan",
                        "amount": 250000,
                    },
                },
            }
        ).encode()
        headers = {"Content-Type": "application/json"}
        status, _ = self.request("/mcp", "POST", payload, headers)
        self.assertEqual(status, 401)
        status, _ = self.request("/mcp/wrong-token", "POST", payload, headers)
        self.assertEqual(status, 401)
        status, result = self.request("/mcp/test-token", "POST", payload, headers)
        self.assertEqual(status, 200)
        self.assertFalse(result["result"]["isError"])
        self.assertEqual(self.store.list_applications()[0]["applicant_name"], "MCP Applicant")
        status, _ = self.request("/mcp/test-token/", "POST", payload, headers)
        self.assertEqual(status, 200)
        status, _ = self.request("/mcp?token=test-token", "POST", payload, headers)
        self.assertEqual(status, 200)

if __name__ == "__main__":
    unittest.main()
