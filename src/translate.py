#!/usr/bin/env python3
"""
Arabic-to-Malayalam Chapter Translation Engine.

Translates Arabic OCR chapters from data/chapters/ into Malayalam TTS-ready
text using a local AIStudioToAPI proxy (OpenAI-compatible endpoint).

Features:
    - Resumable runs via a JSON progress manifest
    - Exponential backoff with jitter on transient failures
    - Model fallback chain (tries models in priority order)
    - Parallel translation with configurable worker count
    - Intelligent text chunking (paragraph-aware, sentence-fallback)
    - Dynamic chapter-specific prompt generation via LLM meta-prompt
    - Post-translation validation (Malayalam purity, bracket/numeral checks)
    - Dry-run mode for previewing work without API calls
    - Configurable chapter ranges (e.g. --chapters 1-20,55,100-108)
    - Real-time progress table printed to the terminal
"""

import os
import sys
import json
import re
import argparse
import time
import random
import urllib.request
import urllib.error
from pathlib import Path
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

MANIFEST_FILENAME = ".translate_manifest.json"
DEFAULT_MODELS = "gemini-3.5-flash-high,gemini-3.5-flash-medium,gemini-3.5-flash-low,gemini-3.5-flash-minimal"
DEFAULT_MAX_CHUNK_CHARS = 15000
DEFAULT_PORT = 7860
DEFAULT_API_KEY = "123456"
DEFAULT_WORKERS = 3

# Retry configuration
MAX_RETRIES_PER_MODEL = 3
INITIAL_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 60.0
BACKOFF_MULTIPLIER = 2.0
JITTER_RANGE = 0.5  # ±50% jitter

# Validation: Malayalam Unicode block U+0D00..U+0D7F, whitespace, punctuation
MALAYALAM_RANGE = r"\u0D00-\u0D7F"
ALLOWED_CHARS_PATTERN = re.compile(
    rf"^[{MALAYALAM_RANGE}\s\.,;:!?\-—–\u200C\u200D\n\r]+$", re.UNICODE
)
BRACKET_PATTERN = re.compile(r"[\(\)\[\]\{\}\u00AB\u00BB]")
NUMERAL_PATTERN = re.compile(r"[0-9\u0660-\u0669\u06F0-\u06F9]")
ARABIC_PATTERN = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")

# Thread safety
_print_lock = Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────────────

class ChapterStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    VALIDATED = "validated"
    VALIDATION_WARN = "validation_warn"


@dataclass
class ChapterRecord:
    """Tracks per-chapter translation state inside the manifest."""
    filename: str
    status: str = ChapterStatus.PENDING.value
    chunks_total: int = 0
    chunks_done: int = 0
    model_used: Optional[str] = None
    char_count_source: int = 0
    char_count_translated: int = 0
    validation_issues: list = field(default_factory=list)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ChapterRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Manifest:
    """Top-level progress manifest persisted to disk."""
    version: int = 1
    created_at: str = ""
    updated_at: str = ""
    input_dir: str = ""
    output_dir: str = ""
    models: list = field(default_factory=list)
    chapters: dict = field(default_factory=dict)  # filename -> ChapterRecord dict

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        m = cls(
            version=d.get("version", 1),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            input_dir=d.get("input_dir", ""),
            output_dir=d.get("output_dir", ""),
            models=d.get("models", []),
        )
        for fname, rec in d.get("chapters", {}).items():
            m.chapters[fname] = rec if isinstance(rec, dict) else rec
        return m


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def log(msg: str, *, error: bool = False):
    """Thread-safe console logging."""
    with _print_lock:
        stream = sys.stderr if error else sys.stdout
        print(msg, file=stream, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_dotenv(project_root: Path):
    """Load variables from .env file into os.environ if not already set."""
    env_path = project_root / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception as e:
        log(f"[warn] Failed to read .env: {e}", error=True)


def parse_chapter_ranges(spec: str, max_chapter: int) -> set[int]:
    """
    Parse a chapter range specification like '1-20,55,100-108' into a set of
    chapter numbers (1-indexed, matching the numeric prefix of filenames).
    """
    result = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo, hi = int(lo.strip()), int(hi.strip())
            result.update(range(lo, min(hi, max_chapter) + 1))
        else:
            result.add(int(part))
    return result


def extract_chapter_number(filename: str) -> Optional[int]:
    """Extract the leading numeric prefix from a chapter filename, e.g. '014_...' -> 14."""
    m = re.match(r"^(\d+)", filename)
    return int(m.group(1)) if m else None


# ─────────────────────────────────────────────────────────────────────────────
# Manifest I/O
# ─────────────────────────────────────────────────────────────────────────────

def load_manifest(output_dir: Path) -> Optional[Manifest]:
    manifest_path = output_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return Manifest.from_dict(data)
    except Exception as e:
        log(f"[warn] Could not load manifest: {e}", error=True)
        return None


def save_manifest(manifest: Manifest, output_dir: Path):
    manifest.updated_at = now_iso()
    manifest_path = output_dir / MANIFEST_FILENAME
    try:
        manifest_path.write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        log(f"[warn] Could not save manifest: {e}", error=True)


_manifest_lock = Lock()


def update_chapter_in_manifest(manifest: Manifest, record: ChapterRecord, output_dir: Path):
    """Thread-safe update of a single chapter record and persist to disk."""
    with _manifest_lock:
        manifest.chapters[record.filename] = record.to_dict()
        save_manifest(manifest, output_dir)


def update_chapter_in_manifest_memory_only(manifest: Manifest, record: ChapterRecord):
    """Thread-safe update of a single chapter record in memory only."""
    with _manifest_lock:
        manifest.chapters[record.filename] = record.to_dict()


def check_proxy_health(port: int, api_key: str = DEFAULT_API_KEY) -> bool:
    """Verify that the AIStudioToAPI proxy is running on localhost."""
    url = f"http://localhost:{port}/v1/models"
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.getcode() == 200
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Text Chunking
# ─────────────────────────────────────────────────────────────────────────────

def chunk_text(text: str, max_chars: int = DEFAULT_MAX_CHUNK_CHARS) -> list[str]:
    """
    Split text into logical chunks without cutting paragraphs.
    Falls back to sentence-level splitting for oversized paragraphs.
    """
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para)

        if para_len > max_chars:
            # Flush accumulated chunk
            if current:
                chunks.append("\n\n".join(current))
                current, current_len = [], 0

            # Split oversized paragraph by sentence (supports Arabic punctuation)
            sentences = re.split(r'(?<=[.؟!۔])\s+', para)
            sent_buf: list[str] = []
            sent_len = 0
            for sent in sentences:
                s_len = len(sent)
                if sent_len + s_len > max_chars and sent_buf:
                    chunks.append(" ".join(sent_buf))
                    sent_buf, sent_len = [sent], s_len
                else:
                    sent_buf.append(sent)
                    sent_len += s_len + 1
            if sent_buf:
                chunks.append(" ".join(sent_buf))

        elif current_len + para_len > max_chars and current:
            chunks.append("\n\n".join(current))
            current, current_len = [para], para_len
        else:
            current.append(para)
            current_len += para_len + 2

    if current:
        chunks.append("\n\n".join(current))
    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# LLM Response Cleaning
# ─────────────────────────────────────────────────────────────────────────────

def clean_llm_response(text: str) -> str:
    """Strip code-block wrappers (```markdown ... ```) if the LLM wraps its output."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) < 2:
        return text
    if lines[0].strip().startswith("```") and lines[-1].strip().startswith("```"):
        return "\n".join(lines[1:-1]).strip()
    if lines[0].strip().startswith("```"):
        return "\n".join(lines[1:]).strip()
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Dynamic Chapter-Specific Prompt Generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_chapter_meta_prompt(
    *,
    content: str,
    chapter_name: str,
    port: int,
    api_key: str,
    model: str,
) -> str:
    """
    Use an LLM to analyse the Arabic chapter content and produce concise,
    chapter-specific translation instructions at runtime.

    Returns the generated notes as plain text, or an empty string on failure.
    """
    meta_system = (
        "You are a pre-translation analysis assistant for classical Arabic Islamic texts. "
        "Analyse the given Arabic chapter and produce concise, actionable translation notes "
        "that will help a translator produce an accurate Arabic-to-Malayalam translation.\n\n"
        "Your notes MUST include (only when relevant):\n"
        "1. CHAPTER THEME: A one-line summary of the chapter's main topic and sub-topics.\n"
        "2. KEY PROPER NOUNS: Every scholar name, place name, and book title found in the text, "
        "   each with its correct transliteration in Malayalam Unicode script.\n"
        "3. KEY TERMS: Important technical, theological, or juristic Arabic terms with their "
        "   standard Malayalam equivalents.\n"
        "4. QURANIC VERSES: List any Quranic verses (often between ﴿ ﴾) with surah name and "
        "   ayah number if identifiable.\n"
        "5. HADITH REFERENCES: Note hadith citations and their narration sources "
        "   (e.g. Bukhari, Muslim, Ahmad).\n"
        "6. TONE GUIDANCE: Any sensitivity or register notes specific to this chapter "
        "   (e.g. contains graphic descriptions, legal rulings, poetic passages).\n\n"
        "Rules:\n"
        "- Output ONLY the notes, no meta-commentary or preamble.\n"
        "- Keep output under 500 words.\n"
        "- Use plain text, no markdown formatting."
    )

    # Send a representative sample: beginning + middle slice
    preview = content[:4000]
    if len(content) > 4000:
        mid = len(content) // 2
        preview += "\n\n[...]\n\n" + content[mid : mid + 2000]

    try:
        raw = call_api(
            port=port,
            api_key=api_key,
            system_prompt=meta_system,
            user_content=preview,
            model=model,
        )
        return clean_llm_response(raw).strip()
    except Exception as e:
        log(f"  [{chapter_name}] ⚠ Dynamic prompt generation failed: {e}", error=True)
        return ""


def build_system_prompt(
    *,
    master_prompt_path: Path,
    chapter_name: str,
    content: str,
    port: int,
    api_key: str,
    model: str,
) -> str:
    """
    Read the master prompt and dynamically generate chapter-specific
    translation instructions via an LLM meta-prompt call.
    """
    system_prompt = master_prompt_path.read_text(encoding="utf-8")

    # Generate chapter-specific prompt dynamically
    log(f"  [{chapter_name}] Generating chapter-specific prompt via LLM...")
    extra = generate_chapter_meta_prompt(
        content=content,
        chapter_name=chapter_name,
        port=port,
        api_key=api_key,
        model=model,
    )

    if extra:
        log(f"  [{chapter_name}] ↳ Dynamic prompt generated ({len(extra)} chars)")
        # Inject inside <context_translations> if the tag exists, otherwise append
        if "</context_translations>" in system_prompt:
            before, after = system_prompt.split("</context_translations>", 1)
            system_prompt = (
                before
                + "\n\n<!-- Chapter-Specific Analysis -->\n"
                + extra
                + "\n</context_translations>"
                + after
            )
        else:
            system_prompt += "\n\n" + extra
    else:
        log(f"  [{chapter_name}] ↳ No dynamic prompt generated, using master prompt only")

    return system_prompt


# ─────────────────────────────────────────────────────────────────────────────
# API Client (AIStudioToAPI / OpenAI-compatible)
# ─────────────────────────────────────────────────────────────────────────────

def call_api(
    *,
    port: int,
    api_key: str,
    system_prompt: str,
    user_content: str,
    model: str,
) -> str:
    """
    Call the local AIStudioToAPI proxy using streaming SSE to avoid timeouts
    on long translations. Returns the assembled response text.
    """
    url = f"http://localhost:{port}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "stream": True,
        "max_tokens": 65536,
        "temperature": 0.2,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=600) as resp:
        if resp.getcode() != 200:
            body = resp.read().decode("utf-8")
            raise RuntimeError(f"HTTP {resp.getcode()}: {body}")

        fragments: list[str] = []
        for raw_line in resp:
            line = raw_line.decode("utf-8").strip()
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            try:
                event = json.loads(data_str)
                delta = event.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    fragments.append(content)
            except (json.JSONDecodeError, IndexError, KeyError):
                pass

        return "".join(fragments)


# ─────────────────────────────────────────────────────────────────────────────
# Retry Logic (Exponential Backoff + Jitter)
# ─────────────────────────────────────────────────────────────────────────────

def translate_chunk_with_retry(
    *,
    port: int,
    api_key: str,
    system_prompt: str,
    chunk_content: str,
    models: list[str],
    chapter_name: str,
    chunk_label: str,
) -> tuple[str, str]:
    """
    Attempt to translate a single chunk. Tries each model with exponential
    backoff before falling back to the next model.

    Returns:
        (translated_text, model_used)

    Raises:
        RuntimeError if all models and retries are exhausted.
    """
    last_error: Optional[Exception] = None

    for model in models:
        backoff = INITIAL_BACKOFF_SECONDS
        for attempt in range(1, MAX_RETRIES_PER_MODEL + 1):
            try:
                log(f"  [{chapter_name}] Translating {chunk_label} → model={model} (attempt {attempt}/{MAX_RETRIES_PER_MODEL})")
                raw = call_api(
                    port=port,
                    api_key=api_key,
                    system_prompt=system_prompt,
                    user_content=chunk_content,
                    model=model,
                )
                cleaned = clean_llm_response(raw)
                if not cleaned:
                    raise ValueError("Empty response from model")
                return cleaned, model

            except urllib.error.HTTPError as e:
                last_error = e
                err_detail = ""
                try:
                    err_detail = e.read().decode("utf-8")
                except Exception:
                    err_detail = str(e.reason)
                log(
                    f"  [{chapter_name}] HTTP {e.code} from {model} on {chunk_label}: {err_detail}",
                    error=True,
                )

            except Exception as e:
                last_error = e
                log(
                    f"  [{chapter_name}] Error from {model} on {chunk_label}: {e}",
                    error=True,
                )

            # Exponential backoff with jitter before next attempt
            if attempt < MAX_RETRIES_PER_MODEL:
                jitter = backoff * random.uniform(-JITTER_RANGE, JITTER_RANGE)
                sleep_time = min(backoff + jitter, MAX_BACKOFF_SECONDS)
                log(f"  [{chapter_name}] Backing off {sleep_time:.1f}s before retry...")
                time.sleep(max(0.1, sleep_time))
                backoff *= BACKOFF_MULTIPLIER

        log(f"  [{chapter_name}] All retries exhausted for {model}, falling back...", error=True)

    raise RuntimeError(
        f"All models failed for {chapter_name} ({chunk_label}): {last_error}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Post-Translation Validation
# ─────────────────────────────────────────────────────────────────────────────

def validate_translation(text: str) -> list[str]:
    """
    Run post-translation quality checks and return a list of issues found.
    An empty list means the translation passed all checks.
    """
    issues: list[str] = []

    # Check for bracket characters
    brackets = BRACKET_PATTERN.findall(text)
    if brackets:
        unique = set(brackets)
        issues.append(f"Found {len(brackets)} bracket character(s): {unique}")

    # Check for numeral characters
    numerals = NUMERAL_PATTERN.findall(text)
    if numerals:
        unique = set(numerals)
        issues.append(f"Found {len(numerals)} numeral character(s): {unique}")

    # Check for Arabic script leakage
    arabic = ARABIC_PATTERN.findall(text)
    if arabic:
        sample = "".join(arabic[:20])
        issues.append(f"Found {len(arabic)} Arabic character(s), sample: {sample}")

    # Check for markdown artifacts
    if re.search(r"^#{1,6}\s", text, re.MULTILINE):
        issues.append("Found Markdown heading syntax (#)")
    if "```" in text:
        issues.append("Found Markdown code block syntax (```)")
    if re.search(r"\*\*.+?\*\*", text):
        issues.append("Found Markdown bold syntax (**)")

    return issues


# ─────────────────────────────────────────────────────────────────────────────
# Single Chapter Translation
# ─────────────────────────────────────────────────────────────────────────────

def translate_chapter(
    *,
    chapter_file: Path,
    output_dir: Path,
    port: int,
    api_key: str,
    master_prompt_path: Path,
    models: list[str],
    max_chunk_chars: int,
    force: bool,
    dry_run: bool,
    manifest: Manifest,
    index: int,
    total: int,
) -> ChapterRecord:
    """Translate a single chapter file, handling chunking, retries, and validation."""
    fname = chapter_file.name
    target_file = output_dir / fname

    record = ChapterRecord(filename=fname)
    record.started_at = now_iso()

    # ── Skip check ──
    if not force and target_file.exists() and target_file.stat().st_size > 0:
        # Only re-process if the manifest explicitly says this chapter failed or was in progress
        with _manifest_lock:
            existing = manifest.chapters.get(fname, {})
        existing_status = existing.get("status") if isinstance(existing, dict) else None
        if existing_status not in (ChapterStatus.FAILED.value, ChapterStatus.IN_PROGRESS.value):
            log(f"  [{index}/{total}] ✓ Skipping {fname} (already translated)")
            record.status = ChapterStatus.SKIPPED.value
            record.finished_at = now_iso()
            return record

    # ── Read source ──
    try:
        content = chapter_file.read_text(encoding="utf-8").strip()
        if not content:
            log(f"  [{index}/{total}] ○ Skipping empty file: {fname}")
            record.status = ChapterStatus.SKIPPED.value
            record.finished_at = now_iso()
            return record
        record.char_count_source = len(content)
    except Exception as e:
        log(f"  [{index}/{total}] ✗ Error reading {fname}: {e}", error=True)
        record.status = ChapterStatus.FAILED.value
        record.error = str(e)
        record.finished_at = now_iso()
        return record

    # ── Dry run ──
    if dry_run:
        chunks = chunk_text(content, max_chunk_chars)
        record.chunks_total = len(chunks)
        record.status = ChapterStatus.PENDING.value
        log(f"  [{index}/{total}] ◇ DRY RUN: {fname} → {len(chunks)} chunk(s), {record.char_count_source} chars")
        record.finished_at = now_iso()
        return record

    # ── Build system prompt (dynamic meta-prompt via LLM) ──
    try:
        system_prompt = build_system_prompt(
            master_prompt_path=master_prompt_path,
            chapter_name=fname,
            content=content,
            port=port,
            api_key=api_key,
            model=models[-1],  # cheapest model in the fallback chain
        )
    except Exception as e:
        log(f"  [{index}/{total}] ✗ Error building prompt for {fname}: {e}", error=True)
        record.status = ChapterStatus.FAILED.value
        record.error = str(e)
        record.finished_at = now_iso()
        return record

    # ── Chunk and translate ──
    record.status = ChapterStatus.IN_PROGRESS.value
    update_chapter_in_manifest(manifest, record, output_dir)

    chunks = chunk_text(content, max_chunk_chars)
    record.chunks_total = len(chunks)
    translated_chunks: list[str] = []
    model_used = None

    for chunk_idx, chunk_content in enumerate(chunks, start=1):
        chunk_label = f"chunk {chunk_idx}/{record.chunks_total}" if record.chunks_total > 1 else "content"
        try:
            translated, model_used = translate_chunk_with_retry(
                port=port,
                api_key=api_key,
                system_prompt=system_prompt,
                chunk_content=chunk_content,
                models=models,
                chapter_name=fname,
                chunk_label=chunk_label,
            )
            translated_chunks.append(translated)
            record.chunks_done = chunk_idx
            record.model_used = model_used
            update_chapter_in_manifest_memory_only(manifest, record)

        except RuntimeError as e:
            log(f"  [{index}/{total}] ✗ Failed {fname} ({chunk_label}): {e}", error=True)
            record.status = ChapterStatus.FAILED.value
            record.error = str(e)
            record.finished_at = now_iso()
            return record

    # ── Combine and save ──
    final = "\n\n".join(translated_chunks)
    record.char_count_translated = len(final)

    try:
        target_file.write_text(final, encoding="utf-8")
    except Exception as e:
        log(f"  [{index}/{total}] ✗ Error saving {target_file.name}: {e}", error=True)
        record.status = ChapterStatus.FAILED.value
        record.error = str(e)
        record.finished_at = now_iso()
        return record

    # ── Validate ──
    issues = validate_translation(final)
    record.validation_issues = issues

    if issues:
        record.status = ChapterStatus.VALIDATION_WARN.value
        log(f"  [{index}/{total}] ⚠ Translated {fname} with {len(issues)} validation warning(s)")
        for issue in issues:
            log(f"      • {issue}")
    else:
        record.status = ChapterStatus.VALIDATED.value
        log(f"  [{index}/{total}] ✓ Translated and validated {fname}")

    record.finished_at = now_iso()
    return record


# ─────────────────────────────────────────────────────────────────────────────
# Progress Display
# ─────────────────────────────────────────────────────────────────────────────

STATUS_SYMBOLS = {
    ChapterStatus.PENDING.value: "○",
    ChapterStatus.IN_PROGRESS.value: "◌",
    ChapterStatus.SUCCESS.value: "✓",
    ChapterStatus.VALIDATED.value: "✓",
    ChapterStatus.VALIDATION_WARN.value: "⚠",
    ChapterStatus.FAILED.value: "✗",
    ChapterStatus.SKIPPED.value: "─",
}


def print_summary_table(manifest: Manifest, elapsed: float):
    """Print a compact summary table of all chapter statuses."""
    counts = {s.value: 0 for s in ChapterStatus}
    for rec in manifest.chapters.values():
        data = rec if isinstance(rec, dict) else rec.to_dict()
        status = data.get("status", ChapterStatus.PENDING.value)
        if status in counts:
            counts[status] += 1

    total = sum(counts.values())
    validated = counts[ChapterStatus.VALIDATED.value]
    warned = counts[ChapterStatus.VALIDATION_WARN.value]
    failed = counts[ChapterStatus.FAILED.value]
    skipped = counts[ChapterStatus.SKIPPED.value]
    pending = counts[ChapterStatus.PENDING.value]

    print()
    print("═" * 60)
    print("  Translation Summary")
    print("═" * 60)
    print(f"  Total chapters:      {total}")
    print(f"  ✓ Validated:         {validated}")
    print(f"  ⚠ Warnings:          {warned}")
    print(f"  ─ Skipped:           {skipped}")
    print(f"  ✗ Failed:            {failed}")
    print(f"  ○ Pending:           {pending}")
    print(f"  Time elapsed:        {elapsed:.1f}s")
    print("═" * 60)

    # Print details for failures and warnings
    for fname, rec in sorted(manifest.chapters.items()):
        data = rec if isinstance(rec, dict) else rec.to_dict()
        status = data.get("status", "")
        if status == ChapterStatus.FAILED.value:
            err = data.get("error", "unknown")
            print(f"  ✗ {fname}: {err}")
        elif status == ChapterStatus.VALIDATION_WARN.value:
            issues = data.get("validation_issues", [])
            print(f"  ⚠ {fname}: {', '.join(issues)}")

    print()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Translate Arabic chapters to Malayalam for TTS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python translate.py                          # Translate all pending chapters\n"
            "  python translate.py --chapters 1-20          # Translate chapters 1 through 20\n"
            "  python translate.py --chapters 14,55,100     # Translate specific chapters\n"
            "  python translate.py --force --chapters 10    # Re-translate chapter 10\n"
            "  python translate.py --dry-run                # Preview without API calls\n"
        ),
    )
    parser.add_argument(
        "-i", "--input-dir",
        help="Source directory with .md chapter files (default: data/chapters/)",
    )
    parser.add_argument(
        "-o", "--output-dir",
        help="Output directory for translations (default: data/translated_chapters/)",
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=None,
        help=f"AIStudioToAPI proxy port (default: {DEFAULT_PORT}, env: PORT)",
    )
    parser.add_argument(
        "-k", "--api-key",
        default=None,
        help=f"API key for the proxy (default: {DEFAULT_API_KEY}, env: API_KEY)",
    )
    parser.add_argument(
        "-w", "--workers",
        type=int,
        default=None,
        help=f"Parallel translation workers (default: {DEFAULT_WORKERS}, env: TRANSLATE_WORKERS)",
    )
    parser.add_argument(
        "-m", "--models",
        default=None,
        help=f"Comma-separated model names in fallback order (default: {DEFAULT_MODELS})",
    )
    parser.add_argument(
        "-c", "--max-chunk-chars",
        type=int,
        default=DEFAULT_MAX_CHUNK_CHARS,
        help=f"Max characters per chunk (default: {DEFAULT_MAX_CHUNK_CHARS})",
    )
    parser.add_argument(
        "--chapters",
        default=None,
        help="Chapter range to translate, e.g. '1-20', '14,55,100-108'",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-translation even if output already exists",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be translated without calling the API",
    )
    parser.add_argument(
        "--reset-manifest",
        action="store_true",
        help="Delete the progress manifest and start fresh",
    )
    return parser


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root)

    parser = build_parser()
    args = parser.parse_args()

    # ── Resolve paths ──
    input_dir = Path(args.input_dir).resolve() if args.input_dir else project_root / "data" / "chapters"
    output_dir = Path(args.output_dir).resolve() if args.output_dir else project_root / "data" / "translated_chapters"
    master_prompt_path = (project_root / "prompts" / "master_prompt.txt").resolve()

    # ── Resolve config with env fallbacks ──
    port = args.port or int(os.environ.get("PORT", DEFAULT_PORT))
    api_key = args.api_key or os.environ.get("API_KEY", DEFAULT_API_KEY)
    workers = args.workers or int(os.environ.get("TRANSLATE_WORKERS", DEFAULT_WORKERS))
    models_str = args.models or os.environ.get("TRANSLATE_MODELS", DEFAULT_MODELS)
    models = [m.strip() for m in models_str.split(",") if m.strip()]

    # ── Validate inputs ──
    if not input_dir.is_dir():
        log(f"Error: Input directory not found: {input_dir}", error=True)
        sys.exit(1)
    if not master_prompt_path.is_file():
        log(f"Error: Master prompt not found: {master_prompt_path}", error=True)
        sys.exit(1)
    if not models:
        log("Error: No models specified.", error=True)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Check proxy health ──
    if not args.dry_run:
        log("Checking proxy health...")
        if not check_proxy_health(port, api_key):
            log(f"Error: AIStudioToAPI proxy is not running on http://localhost:{port}", error=True)
            log("Please make sure the proxy server is started before running translation.", error=True)
            sys.exit(1)
        log("Proxy is alive and responding. ✓")

    # ── Gather chapter files ──
    all_chapters = sorted(input_dir.glob("*.md"))
    if not all_chapters:
        log(f"No .md files found in {input_dir}")
        sys.exit(0)

    # ── Filter by chapter range ──
    if args.chapters:
        max_num = max(
            (extract_chapter_number(f.name) or 0 for f in all_chapters), default=0
        )
        selected = parse_chapter_ranges(args.chapters, max_num)
        chapter_files = [
            f for f in all_chapters
            if extract_chapter_number(f.name) in selected
        ]
        if not chapter_files:
            log(f"No chapters matched range '{args.chapters}'")
            sys.exit(0)
    else:
        chapter_files = all_chapters

    total = len(chapter_files)

    # ── Manifest setup ──
    if args.reset_manifest:
        manifest_path = output_dir / MANIFEST_FILENAME
        if manifest_path.exists():
            manifest_path.unlink()
            log("Manifest reset.")

    manifest = load_manifest(output_dir) or Manifest(
        created_at=now_iso(),
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        models=models,
    )

    # Ensure all chapters have a record in the manifest
    for f in chapter_files:
        if f.name not in manifest.chapters:
            manifest.chapters[f.name] = ChapterRecord(filename=f.name).to_dict()
    save_manifest(manifest, output_dir)

    # ── Banner ──
    mode_label = "DRY RUN" if args.dry_run else "LIVE"
    print()
    print("═" * 60)
    print("  Arabic → Malayalam Translation Engine")
    print("═" * 60)
    print(f"  Mode:            {mode_label}")
    print(f"  Input:           {input_dir}")
    print(f"  Output:          {output_dir}")
    print(f"  Chapters:        {total} / {len(all_chapters)}")
    print(f"  Workers:         {workers}")
    print(f"  Proxy:           http://localhost:{port}")
    print(f"  Models:          {' → '.join(models)}")
    print(f"  Max chunk:       {args.max_chunk_chars} chars")
    print(f"  Force:           {args.force}")
    if args.chapters:
        print(f"  Range filter:    {args.chapters}")
    print("═" * 60)
    print()

    # ── Execute ──
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                translate_chapter,
                chapter_file=f,
                output_dir=output_dir,
                port=port,
                api_key=api_key,
                master_prompt_path=master_prompt_path,
                models=models,
                max_chunk_chars=args.max_chunk_chars,
                force=args.force,
                dry_run=args.dry_run,
                manifest=manifest,
                index=idx,
                total=total,
            ): f
            for idx, f in enumerate(chapter_files, start=1)
        }

        for future in as_completed(futures):
            chapter = futures[future]
            try:
                record = future.result()
                if not args.dry_run:
                    update_chapter_in_manifest(manifest, record, output_dir)
            except Exception as e:
                log(f"Unhandled error for {chapter.name}: {e}", error=True)
                err_record = ChapterRecord(
                    filename=chapter.name,
                    status=ChapterStatus.FAILED.value,
                    error=str(e),
                    finished_at=now_iso(),
                )
                update_chapter_in_manifest(manifest, err_record, output_dir)

    elapsed = time.time() - start_time
    print_summary_table(manifest, elapsed)

    # ── Exit code ──
    has_failures = any(
        (rec if isinstance(rec, dict) else rec.to_dict()).get("status") == ChapterStatus.FAILED.value
        for rec in manifest.chapters.values()
        if (rec if isinstance(rec, dict) else rec.to_dict()).get("filename") in {f.name for f in chapter_files}
    )
    if has_failures:
        log("\nSome chapters failed. Re-run to retry (resume is automatic).", error=True)
        sys.exit(1)
    else:
        print("All chapters processed successfully. ✓")
        sys.exit(0)


if __name__ == "__main__":
    main()
