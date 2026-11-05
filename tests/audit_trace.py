"""
Post-implementation audit: trace actual execution paths to verify correctness.

This script:
1. Verifies tool_result_bytes accuracy
2. Checks overlapping-range read detection
3. Verifies context compaction preserves needed info
4. Checks if truncation can cut critical error/code content
5. Checks duplicate detection in history
6. Checks TOOL_RESULT_LIMIT and CONTEXT_RESULT_LIMIT env var parsing timing
7. Verifies provider usage metadata availability

Run: python tests/audit_trace.py
"""
import builtins
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

for _s in (sys.stdout, sys.stderr):
    try:
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from arcdvncli import agent, tools, llm
from arcdvncli.router import detect_intent, TaskIntent

passed, failed = 0, 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS - {name}")
    else:
        failed += 1
        print(f"  FAIL - {name}: {detail}")


# ============================================================================
# Setup
# ============================================================================
orig_environ = dict(os.environ)
orig_cwd = os.getcwd()
temp_proj = tempfile.mkdtemp()

os.environ["ARCDVN_PROVIDER"] = "google"
os.environ["ARCDVN_GOOGLE_API_KEY"] = "fake-key"
os.environ["ARCDVN_MAX_STEPS"] = "15"

# Remove any ARCDVN_TOOL_RESULT_LIMIT or ARCDVN_CONTEXT_LIMIT so we test defaults
for k in ("ARCDVN_TOOL_RESULT_LIMIT", "ARCDVN_CONTEXT_LIMIT"):
    if k in os.environ:
        del os.environ[k]

os.chdir(temp_proj)
tools.ROOT = Path(temp_proj).resolve()
tools.invalidate_caches()
tools.reset_task_metrics()

orig_input = builtins.input
builtins.input = lambda *a: "y"

try:

    # ========================================================================
    print("\n=== AUDIT 1: tool_result_bytes accuracy ===")
    # Verify that record_tool_result_bytes is called with the truncated length,
    # NOT the original length
    # ========================================================================
    big_file = Path(temp_proj) / "big.txt"
    big_file.write_text("X" * 20000)

    history = []
    messages_captured = []

    def mock_chat_audit1(messages, tools_def=None):
        messages_captured.append(list(messages))
        if len(messages_captured) == 1:
            return {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "big.txt"})}
                }]
            }
        return {"role": "assistant", "content": "Done."}

    with patch("arcdvncli.agent.chat", side_effect=mock_chat_audit1):
        agent.run_agent("baca big.txt", history)

    metrics = agent.get_last_metrics()
    tm = metrics.get("tool_metrics", {})
    tool_bytes = tm.get("tool_result_bytes", 0)

    # TOOL_RESULT_LIMIT defaults to 8000
    # The truncated content is: 8000 chars of content + truncation message (~100 chars)
    check("A1a: bytes tracked match truncated size",
          tool_bytes < 20000 and tool_bytes > 0,
          f"Expected <20000 but got {tool_bytes}")

    # Verify the message content sent to LLM is truncated
    if len(messages_captured) >= 2:
        tool_msgs = [m for m in messages_captured[1] if m.get("role") == "tool"]
        if tool_msgs:
            sent_content = tool_msgs[0].get("content", "")
            check("A1b: content sent to LLM is truncated",
                  len(sent_content) < 20000 and len(sent_content) <= 8200,
                  f"Sent {len(sent_content)} chars to LLM (expected <=~8200)")
            check("A1c: truncation indicator present",
                  "dipotong" in sent_content,
                  "Missing truncation indicator in LLM content")
            check("A1d: bytes matches sent content length",
                  tool_bytes == len(sent_content),
                  f"tool_bytes={tool_bytes} vs sent_content={len(sent_content)}")
        else:
            check("A1b: tool message found", False, "No tool messages in second call")
    else:
        check("A1b: enough LLM calls", False, f"Only {len(messages_captured)} calls")

    # ========================================================================
    print("\n=== AUDIT 2: overlapping read range detection ===")
    # Reading L1-L50 then L25-L75 are overlapping but have different keys
    # ========================================================================
    tools.invalidate_caches()
    tools.reset_task_metrics()

    overlap_file = Path(temp_proj) / "overlap.py"
    lines = [f"line_{i} = {i}" for i in range(100)]
    overlap_file.write_text("\n".join(lines))

    history = []

    def mock_chat_audit2(messages, tools_def=None):
        mock_chat_audit2.count = getattr(mock_chat_audit2, 'count', 0) + 1
        if mock_chat_audit2.count == 1:
            return {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "overlap.py", "start_line": 1, "end_line": 50})}
                }]
            }
        if mock_chat_audit2.count == 2:
            return {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c2",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "overlap.py", "start_line": 25, "end_line": 75})}
                }]
            }
        return {"role": "assistant", "content": "Done."}

    with patch("arcdvncli.agent.chat", side_effect=mock_chat_audit2):
        agent.run_agent("baca overlap.py", history)

    metrics2 = agent.get_last_metrics()
    tm2 = metrics2.get("tool_metrics", {})
    check("A2a: overlapping reads counted as 2 reads",
          tm2.get("read_count", 0) == 2,
          f"read_count={tm2.get('read_count')}")
    check("A2b: overlapping reads NOT flagged as exact repeated",
          tm2.get("repeated_read_count", 0) == 0,
          f"repeated_read_count={tm2.get('repeated_read_count')}")
    check("A2c: [FIXED] overlapping read ranges detected accurately",
          tm2.get("overlapping_read_count", 0) == 1,
          f"overlapping_read_count={tm2.get('overlapping_read_count')}")

    # ========================================================================
    print("\n=== AUDIT 3: context compaction safety ===")
    # Verify compaction doesn't remove system or recent messages
    # ========================================================================
    from arcdvncli.agent import _compact_history_if_needed

    test_msgs = [
        {"role": "system", "content": "SYSTEM_PROMPT" * 100},  # ~1300 chars
        {"role": "user", "content": "Do something"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "read_file", "arguments": '{"path":"x"}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "OLD_RESULT" * 200},  # 2000 chars — should be compacted
        {"role": "assistant", "content": "step done"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t2", "function": {"name": "read_file", "arguments": '{"path":"y"}'}}]},
        {"role": "tool", "tool_call_id": "t2", "content": "OLD_RESULT_2" * 200},  # 2400 chars — should be compacted
        # Recent messages (last 4: indices 7, 8, 9, 10)
        {"role": "assistant", "content": "almost there"},
        {"role": "tool", "tool_call_id": "t3", "content": "RECENT" * 500},  # 3000 chars — should NOT be compacted
        {"role": "assistant", "content": "final"},
        {"role": "user", "content": "next task"},
    ]

    _compact_history_if_needed(test_msgs, limit=3000)

    check("A3a: system message preserved",
          "SYSTEM_PROMPT" in test_msgs[0]["content"],
          "System prompt was modified")
    check("A3b: old tool result (idx 3) was compacted",
          len(test_msgs[3]["content"]) < 2000 and "kompaksi" in test_msgs[3]["content"],
          f"Old tool result not compacted: {len(test_msgs[3]['content'])} chars")
    check("A3c: old tool result (idx 6) was compacted",
          len(test_msgs[6]["content"]) < 2400 and "kompaksi" in test_msgs[6]["content"],
          f"Old tool result not compacted: {len(test_msgs[6]['content'])} chars")
    check("A3d: recent tool result (idx 8) NOT compacted",
          test_msgs[8]["content"] == "RECENT" * 500,
          f"Recent tool result was wrongly compacted: {len(test_msgs[8]['content'])} chars")
    check("A3e: tool_call_id preserved after compaction",
          test_msgs[3].get("tool_call_id") == "t1",
          "tool_call_id lost during compaction")

    # ========================================================================
    print("\n=== AUDIT 4: truncation safety for error messages ===")
    # If a tool produces an error message, truncation must preserve the error
    # ========================================================================
    from arcdvncli.agent import _truncate_tool_result_for_llm

    error_msg = "Tool error: FileNotFoundError: File 'config.yaml' tidak ditemukan"
    truncated = _truncate_tool_result_for_llm(error_msg, limit=8000)
    check("A4a: short error messages not truncated",
          truncated == error_msg,
          f"Short error was truncated to: {truncated[:100]}")

    # Long error with stack trace
    long_error = "Traceback (most recent call last):\n" + ("  at function_call()\n" * 500) + "ValueError: x is invalid"
    truncated_long = _truncate_tool_result_for_llm(long_error, limit=8000)
    check("A4b: long error preserves start (traceback header)",
          truncated_long.startswith("Traceback (most recent call last):"),
          "Lost traceback header")
    check("A4c: long error has truncation notice",
          "dipotong" in truncated_long,
          "Missing truncation indicator")
    # VERIFIED FIX: With head + tail truncation, the final error message at the END is preserved!
    check("A4d: [FIXED] final error line preserved in long trace",
          "ValueError: x is invalid" in truncated_long,
          "Final error line was missing from truncated output")

    # ========================================================================
    print("\n=== AUDIT 5: TOOL_RESULT_LIMIT parsed at import time ===")
    # ========================================================================
    check("A5a: TOOL_RESULT_LIMIT is int",
          isinstance(agent.TOOL_RESULT_LIMIT, int),
          f"Type: {type(agent.TOOL_RESULT_LIMIT)}")
    check("A5b: TOOL_RESULT_LIMIT default is 8000",
          agent.TOOL_RESULT_LIMIT == 8000,
          f"Value: {agent.TOOL_RESULT_LIMIT}")

    # ========================================================================
    print("\n=== AUDIT 6: provider usage metadata parsing ===")
    # ========================================================================
    llm.reset_llm_metrics()
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.json.return_value = {
        "id": "chatcmpl-xxx",
        "choices": [{"message": {"role": "assistant", "content": "test"}}],
        "usage": {
            "prompt_tokens": 150,
            "completion_tokens": 50,
            "total_tokens": 200,
        }
    }

    with patch("requests.post", return_value=mock_response):
        msg = llm.chat([{"role": "user", "content": "test"}])

    llm_m = llm.get_llm_task_metrics()
    check("A6a: [FIXED] llm.py parses usage metadata",
          llm_m["has_usage"] and llm_m["total_tokens"] == 200,
          f"Usage metrics: {llm_m}")
    check("A6b: chat() attaches _usage to message dict",
          "_usage" in msg and msg["_usage"]["prompt_tokens"] == 150,
          f"Message keys: {list(msg.keys())}")

    # ========================================================================
    print("\n=== AUDIT 7: double-counting in record_tool_call vs _TOOL_COUNTS ===")
    # ========================================================================
    tools.reset_task_metrics()
    tools.record_tool_call("read_file")
    tools.record_tool_call("read_file")
    tools.record_tool_call("write_file")
    tm_check = tools.get_task_metrics()
    check("A7a: tool_counts accurate after manual calls",
          tm_check["tool_counts"] == {"read_file": 2, "write_file": 1},
          f"Got: {tm_check['tool_counts']}")

    # ========================================================================
    print("\n=== AUDIT 8: conversation metrics include full schema ===")
    # ========================================================================
    tools.reset_task_metrics()
    llm.reset_llm_metrics()
    history = []
    with patch("arcdvncli.agent.chat") as mock_chat:
        mock_chat.return_value = {"role": "assistant", "content": "Halo!"}
        agent.run_agent("halo", history)

    metrics8 = agent.get_last_metrics()
    check("A8a: conversation metrics contains retry_count",
          "retry_count" in metrics8,
          f"Keys: {list(metrics8.keys())}")
    check("A8b: conversation metrics contains tokens",
          "tokens" in metrics8 and "http_attempts" in metrics8,
          f"Keys: {list(metrics8.keys())}")

    # ========================================================================
    print("\n=== AUDIT 9: cache key mismatch for path-normalized files ===")
    # invalidate_caches(path) filters by str(path) but _FILE_CACHE keys on (str(p), mtime)
    # Verify path matching works correctly
    # ========================================================================
    tools.invalidate_caches()
    tools.reset_task_metrics()

    test_file = Path(temp_proj) / "cached.txt"
    test_file.write_text("v1")

    r1 = tools.read_file("cached.txt")
    check("A9a: initial read works", r1 == "v1", f"Got: {r1}")

    # Now write to it (which calls invalidate_caches with the full resolved path)
    test_file.write_text("v2")
    resolved_p = (tools.ROOT / "cached.txt").resolve()
    tools.invalidate_caches(resolved_p)

    r2 = tools.read_file("cached.txt")
    check("A9b: read after invalidation returns new content",
          r2 == "v2",
          f"Got: {r2} (stale cache!)")

    # Test invalidation with relative path object vs str
    test_file.write_text("v3")
    # invalidate_caches filters by k[0] != p_str where p_str = str(path)
    # The _FILE_CACHE key is (str(p), mtime) where p is the RESOLVED path
    # So invalidate_caches must be called with the resolved path
    tools.invalidate_caches(Path("cached.txt"))  # NOT resolved!
    r3 = tools.read_file("cached.txt")
    # This might be stale because invalidate uses str(Path("cached.txt")) = "cached.txt"
    # but cache key uses str(resolved_path) = "E:\...\cached.txt"
    check("A9c: invalidation with relative path",
          r3 == "v3",
          f"Got: {r3} — STALE CACHE if relative path doesn't match resolved path key")

finally:
    os.chdir(orig_cwd)
    tools.ROOT = Path(orig_cwd).resolve()
    tools.invalidate_caches()
    tools.reset_task_metrics()
    builtins.input = orig_input
    os.environ.clear()
    os.environ.update(orig_environ)
    shutil.rmtree(temp_proj, ignore_errors=True)

print(f"\n{'=' * 60}")
print(f"Audit Results: {passed}/{passed + failed} passed, {failed} failed")
