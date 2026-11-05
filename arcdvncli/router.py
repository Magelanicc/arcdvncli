"""
Lightweight intent router for ARCDVNCLI.
Classifies user requests deterministically without extra LLM round-trips.
Supports Indonesian and English instructions with high precision.
"""
import re


class TaskIntent:
    CONVERSATION = "conversation"      # Cat A: Simple conversation ("halo", "makasih", "apa kabar?")
    EXPLANATION = "explanation"        # Cat B: General questions & explanations ("jelasin apa itu Docker", "apa itu unit test?")
    INSPECTION = "inspection"          # Cat C: Project / file inspection ("jelasin struktur project ini", "cari file index.html")
    CODING = "coding"                  # Cat D: Coding & file modifications ("buat landing page", "perbaiki bug ini")
    COMMAND = "command"                # Cat E: System & command execution ("cek versi Python", "jalankan test", "run pytest")


_MULTI_GREETINGS = [
    "selamat pagi", "selamat siang", "selamat sore", "selamat malam",
    "good morning", "good afternoon", "good evening", "apa kabar", "how are you",
    "terima kasih", "thank you", "siapa kamu", "who are you", "kamu siapa",
    "bisa apa kamu", "what can you do", "test ping",
]

_SINGLE_GREETINGS = {
    "halo", "hai", "hello", "hi", "hey", "hei", "p", "pagi", "siang", "sore",
    "malam", "makasih", "makasi", "thanks", "thx", "ok", "oke", "okay",
    "sip", "mantap", "ping",
}

_PROJECT_ANCHORS = re.compile(
    r"\b(project|proyek|repo|repository|codebase|struktur|direktori|folder|berkas|arsitektur|architecture|disini|di sini|di project|di repo|in this project|in this repo)\b",
    re.IGNORECASE,
)

_CODING_ACTION = re.compile(
    r"\b(buat|buatkan|bikin|bikinin|ciptakan|generate|create|write|tulis|tuliskan|tambah|tambahkan|add|insert|edit|ubah|modifikasi|modify|update|perbaiki|fix|repair|patch|refactor|hapus|delete|remove|hilangkan|ganti|replace|rename|implement|implementasikan|setup|pasang|install|overwrite|timpa|save|simpan)\b",
    re.IGNORECASE,
)

# Explicit command actions (requires an imperative verb or explicit runner/CLI command)
_COMMAND_EXPLICIT_RUN = re.compile(
    r"\b(jalankan|run|execute|eksekusi)\s+(test|tes|pytest|unittest|npm|build|script|perintah|command|server|app)\b|"
    r"\b(pytest|unittest|npm\s+(test|run|start)|python\s+-m\s+unittest|pip\s+install|cargo\s+(test|run|build)|go\s+(test|run))\b|"
    r"\b(cek\s+versi|check\s+version|versi\s+(python|node|npm|git)|(python|node|npm|git)\s+version)\b",
    re.IGNORECASE,
)

_EXPLANATION_STARTERS = re.compile(
    r"^(apa itu|apakah itu|apakah|jelaskan|jelasin|terangkan|bagaimana cara|gimana cara|cara kerja|apa beda|apa perbedaan|apa bedanya|mengapa|kenapa|what is|whats|explain|tell me about|how does|how do|how to|why is|why does|why|difference between)\b",
    re.IGNORECASE,
)

_EXPLANATION_QUESTION_SUFFIX = re.compile(
    r"(itu apa(\s+sih|\s+ya)?|artinya apa|maksudnya apa|is what|mean\??)$",
    re.IGNORECASE,
)

_FILE_NAME_PATTERN = re.compile(
    r"\b[a-zA-Z0-9_\-]+\.(html|css|js|jsx|ts|tsx|py|json|md|txt|csv|xml|yaml|yml|sh|bat|sql|go|rs|c|cpp|h|toml)\b",
    re.IGNORECASE,
)


def detect_intent(text: str) -> str:
    """
    Classify user input into one of five task intents:
    - CONVERSATION: Greetings, thanks, chitchat.
    - EXPLANATION: General tech questions & definitions without project context.
    - INSPECTION: Examining repository structure, inspecting files, searching code.
    - CODING: Creating, modifying, or repairing project files.
    - COMMAND: Executing terminal commands or tests.

    Fast, deterministic heuristic (< 0.1ms). Never downgrades genuine coding/inspection
    tasks to conversation. Avoids false positive command matching on conceptual questions.
    """
    if not text or not str(text).strip():
        return TaskIntent.CONVERSATION

    cleaned = str(text).strip()
    lower = cleaned.lower()

    # 1. Exact greeting or pure chitchat
    clean_punct = re.sub(r"^[^\w]+|[^\w]+$", "", lower)
    if clean_punct in _SINGLE_GREETINGS or clean_punct in _MULTI_GREETINGS:
        return TaskIntent.CONVERSATION

    has_coding_action = bool(_CODING_ACTION.search(lower))
    has_project_anchor = bool(_PROJECT_ANCHORS.search(lower))
    has_file_name = bool(_FILE_NAME_PATTERN.search(lower))

    # 2. Check for conceptual question / explanation patterns FIRST
    # e.g., 'apa itu unit test?', 'test coverage itu apa?', 'kenapa Python lambat?', 'what is a unit test?'
    is_explanation_starter = bool(_EXPLANATION_STARTERS.search(lower))
    is_explanation_suffix = bool(_EXPLANATION_QUESTION_SUFFIX.search(lower.rstrip("?").strip()))
    is_question = lower.endswith("?") or is_explanation_starter or is_explanation_suffix

    # If it is clearly an explanation inquiry and does not ask to modify project files
    if (is_explanation_starter or is_explanation_suffix) and not has_project_anchor and not has_coding_action and not has_file_name:
        return TaskIntent.EXPLANATION

    # Conceptual "why / how / what" question without project anchor or coding request
    if is_question and not has_project_anchor and not has_coding_action and not has_file_name:
        if any(w in lower for w in ["kenapa", "mengapa", "apa", "bagaimana", "gimana", "why", "how", "what", "jelas"]):
            return TaskIntent.EXPLANATION

    # 3. Explicit command execution requests
    # e.g., 'cek versi Python', 'jalankan test', 'run pytest', 'npm test'
    if _COMMAND_EXPLICIT_RUN.search(lower) and not has_coding_action:
        return TaskIntent.COMMAND

    # 4. Coding actions & File creations/modifications
    # e.g., 'buat landing page', 'perbaiki bug di login.py', 'tambah fungsi di app.py'
    has_concrete_file_task = any(verb in lower for verb in [
        "buat", "bikin", "tulis", "tambah", "ubah", "edit", "perbaiki", "fix",
        "create", "write", "add", "modify", "generate", "implement", "overwrite", "timpa",
    ])

    if is_explanation_starter and not has_project_anchor and not has_concrete_file_task and not has_file_name:
        return TaskIntent.EXPLANATION

    if has_coding_action or has_file_name:
        return TaskIntent.CODING

    # 5. Project inspection
    # e.g., 'cek struktur project ini', 'cari penyebab error di file ini', 'jelaskan struktur repo'
    if has_project_anchor and any(w in lower for w in [
        "struktur", "cek", "lihat", "cari", "jelaskan", "jelasin", "baca",
        "isi", "error", "bug", "inspect", "find", "show", "review", "audit",
    ]):
        return TaskIntent.INSPECTION

    # 6. General explanation without project anchors
    if is_explanation_starter and not has_project_anchor:
        return TaskIntent.EXPLANATION

    # 7. Fallbacks:
    if has_project_anchor:
        return TaskIntent.INSPECTION

    # Short conversational fallback with strict word boundaries
    tokens = set(re.findall(r"\b\w+\b", lower))
    has_greeting_token = bool(tokens & _SINGLE_GREETINGS) or any(mg in lower for mg in _MULTI_GREETINGS)
    if len(tokens) <= 4 and has_greeting_token and not any(c in cleaned for c in "(){}[]=<>."):
        return TaskIntent.CONVERSATION

    # Safe fallback: Treat as INSPECTION with tools enabled
    return TaskIntent.INSPECTION
