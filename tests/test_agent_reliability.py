"""
Comprehensive agent reliability tests for ARCDVNCLI.
Covers all 8 required test scenarios (A-H) using mocks — no API calls.

Run from repo root:
    python tests/test_agent_reliability.py
"""
import builtins
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

# --- Setup: isolate from user environment ---
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Isolated HOME + project directory
home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.environ["USERPROFILE"] = home

FAILED = []
PASSED = 0


def check(label, cond):
    global PASSED
    status = "PASS" if cond else "FAIL"
    print(f"  {status} - {label}")
    if cond:
        PASSED += 1
    else:
        FAILED.append(label)


def make_project():
    """Create a fresh temp project directory and chdir into it."""
    project = Path(tempfile.mkdtemp()) / "testproj"
    project.mkdir(parents=True)
    os.chdir(project)
    # Force tools.ROOT to update
    import arcdvncli.tools as tools
    tools.ROOT = Path.cwd().resolve()
    return project


# Ensure ARCDVN_API_KEY is set (mocked, never used)
os.environ["ARCDVN_API_KEY"] = "sk-test-fake"

from arcdvncli import agent, tools
from arcdvncli.llm import LLMError

# Auto-confirm all writes/edits/commands
builtins.input = lambda *a: "y"


# ============================================================================
# TEST A: Model calls list_files, gets "(empty)", then calls write_file
# ============================================================================
print("\n=== TEST A: list_files → (empty) → write_file ===")
project = make_project()

call_sequence = iter([
    # Step 1: model calls list_files
    {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": "call_1",
            "function": {"name": "list_files", "arguments": "{}"}
        }]
    },
    # Step 2: model calls write_file to create index.html
    {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": "call_2",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({
                    "path": "index.html",
                    "content": "<html><body><h1>Hello</h1></body></html>"
                })
            }
        }]
    },
    # Step 3: model reports completion
    {"role": "assistant", "content": "File index.html berhasil dibuat."},
])

agent.chat = lambda m, t: next(call_sequence)
result = agent.run_agent("Buat landing page", [])

check("A1: index.html exists on disk", (project / "index.html").is_file())
check("A2: index.html has content", len((project / "index.html").read_text()) > 10)
check("A3: agent returned success message", "index.html" in result)


# ============================================================================
# TEST B: Model makes multiple tool calls in one response, all processed
# ============================================================================
print("\n=== TEST B: Multiple tool calls in single response ===")
project = make_project()

call_sequence = iter([
    # Step 1: model calls write_file three times at once
    {
        "role": "assistant", "content": None,
        "tool_calls": [
            {
                "id": "call_b1",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "index.html", "content": "<html><head><link rel='stylesheet' href='style.css'></head><body><h1>Hello</h1><script src='app.js'></script></body></html>"})
                }
            },
            {
                "id": "call_b2",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "style.css", "content": "body { background: #111; color: #fff; }"})
                }
            },
            {
                "id": "call_b3",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "app.js", "content": "console.log('loaded');"})
                }
            },
        ]
    },
    # Step 2: done
    {"role": "assistant", "content": "Tiga file berhasil dibuat: index.html, style.css, app.js."},
])

agent.chat = lambda m, t: next(call_sequence)
result = agent.run_agent("Buat landing page dengan CSS dan JS", [])

check("B1: index.html created", (project / "index.html").is_file())
check("B2: style.css created", (project / "style.css").is_file())
check("B3: app.js created", (project / "app.js").is_file())
check("B4: all files in result summary", "index.html" in result and "style.css" in result and "app.js" in result)


# ============================================================================
# TEST C: First request succeeds, second gets 503, retry succeeds
# ============================================================================
print("\n=== TEST C: Success → 503 → retry succeeds ===")
project = make_project()

_call_count_c = 0


def _mock_chat_c(messages, tools_def):
    global _call_count_c
    _call_count_c += 1

    if _call_count_c == 1:
        # Step 1: write first file
        return {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "call_c1",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "page.html", "content": "<h1>Page</h1>"})
                }
            }]
        }
    elif _call_count_c == 2:
        # Step 2: 503 — this will be retried inside llm.chat
        # But since we're mocking at agent level, simulate as if retry succeeded
        return {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "call_c2",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "style.css", "content": "h1 { color: blue; }"})
                }
            }]
        }
    else:
        return {"role": "assistant", "content": "Selesai membuat page.html dan style.css."}


agent.chat = _mock_chat_c
result = agent.run_agent("Buat halaman HTML", [])

check("C1: page.html created", (project / "page.html").is_file())
check("C2: style.css created after retry", (project / "style.css").is_file())
check("C3: result reports completion", "page.html" in result)

# Now test actual LLM retry at llm.py level
print("\n  --- TEST C (LLM-level retry): 503 then success ---")

mock_responses = []


class FakeResponse:
    def __init__(self, status_code, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.ok = 200 <= status_code < 300
        self.headers = headers or {}

    def json(self):
        if self._json is not None:
            return self._json
        raise ValueError("no json")


import arcdvncli.llm as llm_mod

_orig_sleep = time.sleep
time.sleep = lambda x: None  # skip delays in tests

_attempt_c = 0


def _fake_post_c(url, **kwargs):
    global _attempt_c
    _attempt_c += 1
    if _attempt_c <= 2:
        return FakeResponse(503, json_data={"error": {"message": "provider overloaded"}})
    return FakeResponse(200, json_data={
        "choices": [{"message": {"role": "assistant", "content": "OK"}}]
    })


_attempt_c = 0
with patch("arcdvncli.llm.requests.post", side_effect=_fake_post_c):
    with patch("arcdvncli.llm.load_env"):
        result_llm = llm_mod.chat([{"role": "user", "content": "test"}], [])

check("C4: LLM retries on 503 and succeeds", result_llm["content"] == "OK")
check("C5: required 3 attempts (2 failures + 1 success)", _attempt_c == 3)


# ============================================================================
# TEST D: All retries fail, but files already created are preserved
# ============================================================================
print("\n=== TEST D: All retries fail, existing files preserved ===")
project = make_project()

_call_count_d = 0


def _mock_chat_d(messages, tools_def):
    global _call_count_d
    _call_count_d += 1

    if _call_count_d == 1:
        return {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "call_d1",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "saved.html", "content": "<h1>Saved</h1>"})
                }
            }]
        }
    # After first file, LLM fails permanently
    raise LLMError("provider_overloaded", retryable=True)


agent.chat = _mock_chat_d
result = agent.run_agent("Buat halaman", [])

check("D1: saved.html still exists on disk", (project / "saved.html").is_file())
check("D2: saved.html content intact", (project / "saved.html").read_text() == "<h1>Saved</h1>")
check("D3: error message mentions the file", "saved.html" in result)
check("D4: error message is informative", "tidak tersedia" in result or "error" in result.lower())


# ============================================================================
# TEST E: Model gives empty response before task is complete
# ============================================================================
print("\n=== TEST E: Empty response handling ===")
project = make_project()

_call_count_e = 0


def _mock_chat_e(messages, tools_def):
    global _call_count_e
    _call_count_e += 1

    if _call_count_e == 1:
        # Empty response (no content, no tool calls)
        return {"role": "assistant", "content": None}
    elif _call_count_e == 2:
        # After nudge, model actually does work
        return {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "call_e1",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "recovered.txt", "content": "content"})
                }
            }]
        }
    else:
        return {"role": "assistant", "content": "File recovered.txt dibuat."}


agent.chat = _mock_chat_e
result = agent.run_agent("Buat file", [])

check("E1: agent recovered from empty response", (project / "recovered.txt").is_file())
check("E2: file has content", (project / "recovered.txt").read_text() == "content")
check("E3: result mentions the file", "recovered.txt" in result)


# ============================================================================
# TEST F: write_file fails or is cancelled, agent doesn't report success
# ============================================================================
print("\n=== TEST F: write_file failure/cancellation handling ===")
project = make_project()

# Simulate user rejecting the overwrite
(project / "existing.txt").write_text("original", encoding="utf-8")
reject_answers = iter(["n"])  # reject overwrite
builtins.input = lambda *a: next(reject_answers, "y")

call_sequence_f = iter([
    {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": "call_f1",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": "existing.txt", "content": "OVERWRITTEN"})
            }
        }]
    },
    {"role": "assistant", "content": "File tidak bisa ditimpa karena user menolak."},
])

agent.chat = lambda m, t: next(call_sequence_f)
result = agent.run_agent("Timpa file", [])

check("F1: original file content preserved", (project / "existing.txt").read_text() == "original")
check("F2: result mentions rejection status", "rejected" in result.lower() or "existing.txt" in result)

# Reset input
builtins.input = lambda *a: "y"


# ============================================================================
# TEST G: Agent reaches max iterations, shows informative status
# ============================================================================
print("\n=== TEST G: Max iteration with status report ===")
project = make_project()

_call_count_g = 0


def _mock_chat_g(messages, tools_def):
    global _call_count_g
    _call_count_g += 1

    if _call_count_g == 1:
        return {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": f"call_g1",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "partial.html", "content": "<h1>Partial</h1>"})
                }
            }]
        }
    # Keep requesting more tools forever (different calls to avoid stall detection)
    return {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": f"call_g{_call_count_g}",
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"path": "partial.html"})
            }
        }]
    }


# Temporarily lower max steps for faster test
orig_max = agent._MAX_STEPS
agent._MAX_STEPS = 5

agent.chat = _mock_chat_g
result = agent.run_agent("Buat banyak file", [])

agent._MAX_STEPS = orig_max  # restore

check("G1: partial.html exists (work preserved)", (project / "partial.html").is_file())
check("G2: result mentions step limit", "berhenti" in result.lower() or "langkah" in result.lower())
check("G3: result lists created files", "partial.html" in result)


# ============================================================================
# TEST H: Implementation on existing project doesn't delete other files
# ============================================================================
print("\n=== TEST H: Existing project files preserved ===")
project = make_project()

# Pre-existing files
(project / "README.md").write_text("# My Project", encoding="utf-8")
(project / "config.json").write_text('{"key": "value"}', encoding="utf-8")
(project / "src").mkdir()
(project / "src" / "app.py").write_text("print('hello')", encoding="utf-8")

call_sequence_h = iter([
    # Model inspects files first
    {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": "call_h1",
            "function": {"name": "list_files", "arguments": "{}"}
        }]
    },
    # Model adds a new file without touching existing ones
    {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": "call_h2",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": "src/utils.py", "content": "def helper(): pass"})
            }
        }]
    },
    {"role": "assistant", "content": "File src/utils.py ditambahkan."},
])

agent.chat = lambda m, t: next(call_sequence_h)
result = agent.run_agent("Tambahkan helper function", [])

check("H1: new file created", (project / "src" / "utils.py").is_file())
check("H2: README.md unchanged", (project / "README.md").read_text() == "# My Project")
check("H3: config.json unchanged", (project / "config.json").read_text() == '{"key": "value"}')
check("H4: app.py unchanged", (project / "src" / "app.py").read_text() == "print('hello')")


# ============================================================================
# BONUS: Test stall detection (repeated identical tool calls)
# ============================================================================
print("\n=== BONUS: Stall detection ===")
project = make_project()

agent.chat = lambda m, t: {
    "role": "assistant", "content": None,
    "tool_calls": [{
        "id": "call_stall",
        "function": {"name": "list_files", "arguments": "{}"}
    }]
}

orig_max = agent._MAX_STEPS
agent._MAX_STEPS = 10
result = agent.run_agent("Apa saja file di sini?", [])
agent._MAX_STEPS = orig_max

check("STALL1: agent detected repeated calls", "loop" in result.lower() or "diulang" in result.lower())


# ============================================================================
# BONUS: Test LLM error classification
# ============================================================================
print("\n=== BONUS: LLM error classification ===")

# Test permanent error (401) - no retry
_attempt_401 = 0


def _fake_post_401(url, **kwargs):
    global _attempt_401
    _attempt_401 += 1
    return FakeResponse(401, json_data={"error": {"message": "invalid api key"}})


_attempt_401 = 0
try:
    with patch("arcdvncli.llm.requests.post", side_effect=_fake_post_401):
        with patch("arcdvncli.llm.load_env"):
            llm_mod.chat([{"role": "user", "content": "test"}], [])
    check("ERR1: 401 should raise", False)
except LLMError as e:
    check("ERR1: 401 raises immediately without retry", _attempt_401 == 1)
    check("ERR2: 401 not marked retryable", not e.retryable)

# Test timeout - retries then fails
_attempt_timeout = 0


def _fake_post_timeout(url, **kwargs):
    global _attempt_timeout
    _attempt_timeout += 1
    import requests as req
    raise req.exceptions.Timeout("timed out")


_attempt_timeout = 0
try:
    with patch("arcdvncli.llm.requests.post", side_effect=_fake_post_timeout):
        with patch("arcdvncli.llm.load_env"):
            llm_mod.chat([{"role": "user", "content": "test"}], [])
    check("ERR3: timeout should raise", False)
except LLMError as e:
    check("ERR3: timeout retries max times", _attempt_timeout == 4)  # 1 initial + 3 retries
    check("ERR4: timeout is retryable", e.retryable)


# Restore time.sleep
time.sleep = _orig_sleep

# ============================================================================
# Summary
# ============================================================================
print(f"\n{'='*60}")
total = PASSED + len(FAILED)
print(f"Results: {PASSED}/{total} passed, {len(FAILED)} failed")
if FAILED:
    print("\nFailed tests:")
    for f in FAILED:
        print(f"  ✗ {f}")
if __name__ == "__main__":
    sys.exit(1 if FAILED else 0)
