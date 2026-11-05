"""
Comprehensive regression tests for Intent Router in ARCDVNCLI.
Validates Indonesian and English natural language queries.
Ensures conceptual questions containing command/code keywords are NOT false-positively
classified as COMMAND or CODING.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from arcdvncli.router import detect_intent, TaskIntent


class TestRouterRegression(unittest.TestCase):

    # ------------------------------------------------------------------------
    # 1. Critical False Positive Fixes (User questions with keywords)
    # ------------------------------------------------------------------------
    def test_conceptual_questions_with_test_keyword(self):
        """Questions containing 'test' must be EXPLANATION, never COMMAND."""
        cases = [
            "apa itu unit test?",
            "test coverage itu apa?",
            "apakah itu unit test?",
            "jelaskan apa itu integration test",
            "apa bedanya unit test dan e2e test?",
            "bagaimana cara membuat unit test yang baik?",
            "what is unit testing?",
            "what is a test fixture?",
            "explain how pytest works",
            "why is test coverage important?",
            "how do unit tests work in Python?",
        ]
        for query in cases:
            intent = detect_intent(query)
            self.assertEqual(
                intent,
                TaskIntent.EXPLANATION,
                f"Query '{query}' was falsely classified as '{intent}' instead of EXPLANATION",
            )

    def test_conceptual_questions_with_language_keywords(self):
        """Questions containing 'python', 'file', 'code' must be EXPLANATION."""
        cases = [
            "kenapa Python lambat?",
            "mengapa Python menggunakan GIL?",
            "apa bedanya list dan tuple di Python?",
            "gimana cara kerja virtual environment di Python?",
            "what is the difference between a process and a thread?",
            "why does Python have dynamic typing?",
            "explain async and await in javascript",
        ]
        for query in cases:
            intent = detect_intent(query)
            self.assertEqual(
                intent,
                TaskIntent.EXPLANATION,
                f"Query '{query}' was falsely classified as '{intent}' instead of EXPLANATION",
            )

    # ------------------------------------------------------------------------
    # 2. Genuine Command Execution Queries
    # ------------------------------------------------------------------------
    def test_genuine_command_requests(self):
        """Explicit command and execution requests must be classified as COMMAND."""
        cases = [
            "cek versi Python",
            "check version python",
            "versi python",
            "python version",
            "jalankan test",
            "run pytest",
            "eksekusi test",
            "python -m unittest",
            "npm test",
            "npm run build",
            "cargo test",
            "go test",
        ]
        for query in cases:
            intent = detect_intent(query)
            self.assertEqual(
                intent,
                TaskIntent.COMMAND,
                f"Command query '{query}' was classified as '{intent}' instead of COMMAND",
            )

    # ------------------------------------------------------------------------
    # 3. Genuine Coding & Modification Requests
    # ------------------------------------------------------------------------
    def test_genuine_coding_requests(self):
        """File modification, creation, or bug repair requests must be CODING."""
        cases = [
            "buat landing page dengan HTML dan CSS",
            "bikin fungsi login di auth.py",
            "tambahkan endpoint /users di api.py",
            "perbaiki bug TypeError di calc.py",
            "fix error di index.js",
            "create a new script runner.py",
            "write tests for utils.py",
            "refactor database connection in db.py",
            "hapus file temp.txt",
        ]
        for query in cases:
            intent = detect_intent(query)
            self.assertEqual(
                intent,
                TaskIntent.CODING,
                f"Coding query '{query}' was classified as '{intent}' instead of CODING",
            )

    # ------------------------------------------------------------------------
    # 4. Project Inspection Queries
    # ------------------------------------------------------------------------
    def test_project_inspection_requests(self):
        """Inspection of project structure, files, or codebase must be INSPECTION."""
        cases = [
            "cek struktur project ini",
            "jelaskan struktur repo ini",
            "lihat isi folder src",
            "cari file konfigurasi di project ini",
            "inspect project architecture",
            "review codebase structure",
            "audit repo dependencies",
        ]
        for query in cases:
            intent = detect_intent(query)
            self.assertEqual(
                intent,
                TaskIntent.INSPECTION,
                f"Inspection query '{query}' was classified as '{intent}' instead of INSPECTION",
            )

    # ------------------------------------------------------------------------
    # 5. Pure Conversation & Greetings
    # ------------------------------------------------------------------------
    def test_conversation_and_greetings(self):
        """Greetings and casual chitchat must be CONVERSATION."""
        cases = [
            "halo",
            "hai",
            "hello",
            "selamat pagi",
            "apa kabar",
            "makasih",
            "thank you",
            "siapa kamu",
            "ping",
            "ok",
            "mantap",
        ]
        for query in cases:
            intent = detect_intent(query)
            self.assertEqual(
                intent,
                TaskIntent.CONVERSATION,
                f"Greeting '{query}' was classified as '{intent}' instead of CONVERSATION",
            )


if __name__ == "__main__":
    unittest.main()

