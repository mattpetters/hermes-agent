"""Workspace config keys, defaults, and path helpers.

Zero internal dependencies — safe to import from anywhere.
Both workspace/ modules and hermes_cli/config.py import from here.
"""

from __future__ import annotations

from pathlib import Path

BINARY_SUFFIXES: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".ico",
        ".svg",
        ".zip",
        ".gz",
        ".tar",
        ".xz",
        ".7z",
        ".bz2",
        ".rar",
        ".mp3",
        ".wav",
        ".ogg",
        ".flac",
        ".aac",
        ".mp4",
        ".mov",
        ".avi",
        ".mkv",
        ".webm",
        ".pdf",
        ".docx",
        ".doc",
        ".pptx",
        ".xlsx",
        ".sqlite",
        ".db",
        ".bin",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".wasm",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".pyc",
        ".pyo",
        ".class",
        ".o",
        ".obj",
        ".a",
        ".lib",
        ".DS_Store",
        ".lock",
    }
)

CODE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".rs",
        ".go",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".kt",
        ".scala",
        ".lua",
        ".r",
        ".m",
        ".mm",
        ".zig",
        ".nim",
        ".ex",
        ".exs",
        ".erl",
        ".hs",
        ".ml",
        ".mli",
        ".clj",
        ".cljs",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".bat",
        ".cmd",
        ".sql",
        ".graphql",
        ".proto",
        ".thrift",
    }
)

MARKDOWN_SUFFIXES: frozenset[str] = frozenset(
    {
        ".md",
        ".mdx",
        ".markdown",
        ".mdown",
        ".mkd",
    }
)

WORKSPACE_SUBDIRS = ("docs", "notes", "data", "code", "uploads", "media")

WORKSPACE_CONFIG_DEFAULTS = {
    "enabled": True,
    "path": "",
}

KNOWLEDGEBASE_CONFIG_DEFAULTS = {
    "roots": [],
    "chunking": {
        "chunk_size": 512,
        "overlap": None,
    },
    "indexing": {
        "max_file_mb": 10,
    },
    "search": {
        "default_limit": 20,
    },
}

CHUNKING_PLAN_VERSION = "v2"

INDEX_DIR_NAME = ".index"
INDEX_DB_NAME = "workspace.sqlite"
HERMESIGNORE_NAME = ".hermesignore"
GITIGNORE_NAME = ".gitignore"

DEFAULT_IGNORE_PATTERNS = """\
# Version control
.git/
.svn/
.hg/

# OS files
.DS_Store
Thumbs.db
Desktop.ini

# IDE / editor
.idea/
.vscode/
*.swp
*.swo
*~

# Python
__pycache__/
*.pyc
*.pyo
.tox/
.venv/
venv/
.env/
*.egg-info/
.eggs/
dist/
build/

# JavaScript / Node
node_modules/
bower_components/
.npm/
.yarn/

# Build outputs
target/
out/
_build/

# Hermes internals
.index/
.hermesignore
"""


def get_workspace_root(hermes_home: Path, workspace_path: str = "") -> Path:
    if workspace_path:
        return Path(workspace_path).expanduser().resolve()
    return hermes_home / "workspace"


def get_index_dir(workspace_root: Path) -> Path:
    return workspace_root / INDEX_DIR_NAME


def get_index_db_path(workspace_root: Path) -> Path:
    return get_index_dir(workspace_root) / INDEX_DB_NAME
