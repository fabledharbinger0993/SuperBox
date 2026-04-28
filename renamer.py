"""
rekordbox-toolkit / renamer.py

Batch-renames audio files in a directory based on their ID3/Vorbis tags,
generating clean filenames with smart artist prioritization.

This tool extracts metadata (artist, title) from tags and replaces
underscores, numbers, and processing suffixes with a standardized format.
All metadata remains in the ID3 tags for database searchability.

Design:
  - Reads metadata via mutagen (same as scanner.py)
  - Artist priority: vocal/lead (TPE1) > album artist/band (TPE2) > fallback
  - Generates clean filenames: "{Artist}: {Title}.{ext}" or "{Artist}: {Title} (2).{ext}"
  - Preserves copy markers: (2), (3), (copy), (duplicate), (v2) from original filename
  - Falls back to original filename if title missing
  - Handles collisions: if file exists, uses (2), (3), ... until free slot found
  - Updates rekordbox DjmdContent.FolderPath for each renamed file
  - No file moves — renames happen in place
  - Dry-run mode by default; pass dry_run=False to execute

Supported naming patterns (detected and cleaned):
  - "SomethingPN.mp3" or "Something_PN.mp3" → extracts title, removes PN
  - "918223_SomethingElse.mp3" → extracts title, removes ID prefix
  - "Something_918223.mp3" → extracts title, removes ID suffix
  - "Track (remix).mp3" or "Track (dub).mp3" → preserves remix/version markers
  - "Track (2).mp3" → preserves copy marker as "Artist: Track (2).mp3"
  - Remixes: Uses original artist, preserves remixer in title marker
    E.g., "Donna Summer: On the Radio (Felix da-Housecat remix)"
  - Standard "Artist - Title.mp3" → extracted with artist prioritization
  - Anything else → fallback to original name

Artist Priority Examples:
  - If both vocal artist (TPE1) and album artist (TPE2) exist → uses vocal artist
  - If only album artist exists → uses band name
  - If only producer/release artist exists → uses producer name
  - If nothing in tags → tries filename parsing

Copy Suffix Examples:
  - "Track (2).mp3" → "Artist: Track (2).mp3"
  - "Remix (copy).mp3" → "Artist: Remix (copy).mp3"
  - Duplicates after rename: "Artist: Track (2).mp3", "Artist: Track (3).mp3", etc.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TYPE_CHECKING

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, ID3NoHeaderError

from config import AUDIO_EXTENSIONS, BATCH_SIZE, SKIP_DIRS, SKIP_PREFIXES
import renamer_learned as _learned
from scanner import extract_metadata

if TYPE_CHECKING:
    from pyrekordbox.db6.tables import DjmdContent

log = logging.getLogger(__name__)

# Patterns to detect and clean from filenames
_PN_SUFFIX = re.compile(r'_?PN\s*\d*$', re.IGNORECASE)  # "Something_PN" or "SomethingPN2"
_ID_PREFIX = re.compile(r'^\d{6,}\s*[-_.]')             # "918223_Title" or "918223-Title"
_ID_SUFFIX = re.compile(r'[-_\.]\d{6,}$')               # "Title_918223" or "Title-918223"
_UNDERSCORE = re.compile(r'_')                          # Underscores (replaced with spaces)
_MULTI_SPACE = re.compile(r'\s{2,}')                    # Multiple spaces
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|]')             # Filesystem-unsafe
_COPY_SUFFIX = re.compile(r'\s*\((\d+|copy|duplicate|v\d+)\)\s*$', re.IGNORECASE)  # "(2)", "(copy)", etc.
_VERSION_MARKERS = re.compile(
    r'\((remix|dub|extended|acoustic|instrumental|version|edit|remix[\s\-]mix|remaster|radio[\s\-]edit)\)',
    re.IGNORECASE
)                                                        # Version/remix markers to preserve
_NO_NAME_FOLDER = "No-Name tracks for Tagging"
_NO_NAME_MANIFEST = "_quarantine_manifest.json"


def _is_key_token(token: str) -> bool:
    return bool(re.fullmatch(r"\d{1,2}[ABab]", token.strip()))


def _is_key_or_bpm_chunk(chunk: str) -> bool:
    """True for segments like '8A 9A', '8A: 8A', or '124'."""
    c = chunk.strip()
    if not c:
        return False
    if re.fullmatch(r"\d{2,3}", c):
        return True

    # Remove punctuation separators and inspect tokens.
    tokens = [t for t in re.split(r"[\s:._-]+", c) if t]
    if not tokens:
        return False

    key_like = [t for t in tokens if _is_key_token(t)]
    if key_like and len(key_like) == len(tokens):
        return True

    return False


def _strip_leading_key_bpm_prefix(text: str) -> str:
    """Drop leading key/BPM marker chunks separated by ' - '."""
    chunks = [c.strip() for c in text.split(" - ")]
    while chunks and _is_key_or_bpm_chunk(chunks[0]):
        chunks.pop(0)
    return " - ".join(chunks).strip() if chunks else text.strip()


def _looks_like_junk_artist(text: str | None) -> bool:
    if not text:
        return True
    s = text.strip()
    if not s:
        return True
    if _is_key_or_bpm_chunk(s):
        return True
    if re.search(r"(?:^|\s)_?PN(?:\s|\d|$)", s, flags=re.IGNORECASE):
        return True
    if re.search(r"\b\d{2,3}\b", s) and re.search(r"\b\d{1,2}[ABab]\b", s):
        return True
    return False


def _normalize_artist_text(raw: str | None) -> str | None:
    """Normalize and de-duplicate repeated artist strings."""
    if not raw:
        return None
    s = _MULTI_SPACE.sub(" ", str(raw)).strip()
    if not s:
        return None

    # Normalize separators so repeated chunks are easier to detect.
    s = re.sub(r'\s*[/|;,]+\s*', ' ', s)
    s = _MULTI_SPACE.sub(" ", s).strip()

    # Collapse repeated token sequences of any length:
    #   "Gayle Adams Gayle Adams" -> "Gayle Adams"
    #   "DJ PP Jack Mood DJ PP Jack Mood DJ PP Jack Mood" -> "DJ PP Jack Mood"
    tokens = s.split()
    n = len(tokens)
    for unit_len in range(1, (n // 2) + 1):
        if n % unit_len != 0:
            continue
        unit = tokens[:unit_len]
        repeats = n // unit_len
        if repeats > 1 and unit * repeats == tokens:
            s = " ".join(unit)
            break

    return s or None


def _canon(s: str) -> str:
    """Case/spacing/punctuation-insensitive string form for comparisons."""
    s = s.casefold()
    s = re.sub(r'[^a-z0-9]+', '', s)
    return s


def _strip_leading_artist_from_title(artist: str, title: str) -> str:
    """
    If title already starts with the same artist text, strip that prefix.

    Examples:
      "Jamiroquai - Too Young to Die" -> "Too Young to Die"
      "Jamiroquai: Too Young to Die" -> "Too Young to Die"
      "Jamiroquai Too Young to Die"  -> "Too Young to Die"
    """
    a_raw = _normalize_artist_text(artist) or _MULTI_SPACE.sub(" ", artist).strip()
    t = _MULTI_SPACE.sub(" ", title).strip()
    if not a_raw or not t:
        return title

    # Remove repeated leading artist chunks (with optional separators) until clear.
    remainder = t
    while True:
        m = re.match(rf"^\s*{re.escape(a_raw)}\s*(?:[-_:;|/\\]+\s*)?(?P<rest>.*)$", remainder, flags=re.IGNORECASE)
        if not m:
            break
        nxt = (m.group("rest") or "").strip()
        if not nxt or _canon(nxt) == _canon(remainder):
            break
        remainder = nxt

    # Also handle title forms like "Artist Artist - Title" where canonical prefix matches.
    if _canon(remainder).startswith(_canon(a_raw)):
        m2 = re.match(rf"^\s*{re.escape(a_raw)}\b\s*(?P<rest>.*)$", remainder, flags=re.IGNORECASE)
        if m2:
            remainder = (m2.group("rest") or "").lstrip(" -_:;|/\\\t")

    return remainder if remainder else title


def _get_prioritized_artist(path: Path) -> str | None:
    """
    Read artist tags from the file and return the highest-priority artist.
    
    Priority order (use first available):
    1. TPE1 (Lead/Vocal artist) — the vocalist or primary performer
    2. TPE2 (Album artist/Band) — the band or ensemble name
    3. Fall back to None
    
    For remixes: Returns the original artist, not the remixer.
    E.g., "Donna Summer" (not "Felix da Housecat") for a remix.
    
    Returns: Cleaned artist string or None.
    """
    try:
        mf = MutagenFile(path)
        if not mf or not mf.tags:
            return None
        
        tags = mf.tags
        
        # ID3 tags (MP3, AIFF, WAV with ID3)
        if isinstance(tags, ID3):
            # TPE1: Lead/Vocal artist
            tpe1 = tags.get('TPE1')
            if tpe1 is not None:
                text = getattr(tpe1, 'text', None)
                if text:
                    s = _normalize_artist_text(str(text[0]).strip())
                    if s:
                        return s
                s = _normalize_artist_text(str(tpe1).strip())
                if s:
                    return s
            # TPE2: Album artist (band, ensemble)
            tpe2 = tags.get('TPE2')
            if tpe2 is not None:
                text = getattr(tpe2, 'text', None)
                if text:
                    s = _normalize_artist_text(str(text[0]).strip())
                    if s:
                        return s
                s = _normalize_artist_text(str(tpe2).strip())
                if s:
                    return s
        
        # Vorbis comments (FLAC, OGG, Opus)
        elif hasattr(tags, 'get'):
            # Vorbis ARTIST (vocalist/lead)
            artist = tags.get('artist')
            if artist and isinstance(artist, list) and artist[0].strip():
                s = _normalize_artist_text(artist[0].strip())
                if s:
                    return s
            # Vorbis ALBUMARTIST (band/ensemble)
            album_artist = tags.get('albumartist')
            if album_artist and isinstance(album_artist, list) and album_artist[0].strip():
                s = _normalize_artist_text(album_artist[0].strip())
                if s:
                    return s
        
        return None
    except Exception as e:
        log.debug(f"Could not read artist tags from {path}: {e}")
        return None


@dataclass
class RenameResult:
    """Outcome of a single file rename."""
    original_path: Path
    new_path: Path | None
    action: str  # "renamed" | "skipped" | "collision_numbered" | "error" | "no_change"
    reason: str = ""
    content_id: str | None = None


@dataclass
class ProbeCandidate:
    source_path: Path
    proposed_artist: str | None
    proposed_title: str | None
    proposed_mix: str | None
    proposed_copy_suffix: str | None
    proposed_filename: str
    score: int
    reasons: list[str]

    def to_dict(self) -> dict:
        return {
            "source_path": str(self.source_path),
            "source_name": self.source_path.name,
            "proposed_artist": self.proposed_artist,
            "proposed_title": self.proposed_title,
            "proposed_mix": self.proposed_mix,
            "proposed_copy_suffix": self.proposed_copy_suffix,
            "proposed_filename": self.proposed_filename,
            "score": self.score,
            "reasons": list(self.reasons),
        }


def _looks_like_junk_title(text: str | None) -> bool:
    if not text:
        return True
    s = text.strip()
    if not s:
        return True
    if _is_key_or_bpm_chunk(s):
        return True
    if re.fullmatch(r"unknown|untitled|track\s*\d*", s, flags=re.IGNORECASE):
        return True
    return False


def _label_code(stem: str) -> str | None:
    match = re.match(r"^([A-Za-z]{2,6}\d{2,5})\b", stem.strip())
    return match.group(1).upper() if match else None


def _strip_release_junk(stem: str) -> str:
    stem = _COPY_SUFFIX.sub("", stem).strip()
    stem = _PN_SUFFIX.sub("", stem).strip()
    stem = _ID_PREFIX.sub("", stem).strip()
    stem = _ID_SUFFIX.sub("", stem).strip()
    stem = _strip_leading_key_bpm_prefix(stem)
    stem = _UNDERSCORE.sub(" ", stem).strip()
    stem = _MULTI_SPACE.sub(" ", stem).strip()
    return stem


def _extract_mix_annotation(text: str) -> tuple[str, str | None]:
    match = re.search(r"\s*\(([^()]+)\)\s*$", text)
    if not match:
        return text.strip(), None
    inside = match.group(1).strip()
    if re.fullmatch(r"\d+|copy|duplicate|v\d+", inside, flags=re.IGNORECASE):
        return text.strip(), None
    return text[:match.start()].strip(), inside


def _stratified_sample(files: list[Path], k: int) -> list[Path]:
    n = len(files)
    if n <= k:
        return list(files)
    step = n / k
    indexes: list[int] = []
    seen: set[int] = set()
    for i in range(k):
        idx = int(i * step)
        if idx not in seen:
            indexes.append(idx)
            seen.add(idx)
    return [files[i] for i in indexes]


def _canonicalize_mix_annotation(mix_annotation: str | None, rules: "_learned.LearnedRules | None") -> str | None:
    if not mix_annotation:
        return None
    text = _MULTI_SPACE.sub(" ", mix_annotation).strip()
    if not rules:
        return text

    alias = rules.producer_alias(text)
    if alias:
        return alias

    match = re.match(r"^(?P<name>.+?)\s+(?P<suffix>remix|dub|edit|mix|rework|version|remaster|bootleg|re-edit|radio\s+edit|extended\s+mix)$", text, flags=re.IGNORECASE)
    if match:
        name = match.group("name").strip()
        suffix = match.group("suffix").strip()
        canonical = rules.canonical_producer(name)
        alias = rules.producer_alias(name)
        if canonical:
            return f"{canonical} {suffix}"
        if alias:
            return f"{alias} {suffix}"
    return text


def _extract_known_producer_tail(title: str | None, rules: "_learned.LearnedRules | None") -> tuple[str | None, str | None]:
    if not title or not rules:
        return title, None
    tokens = [token for token in re.split(r"\s+", title.strip()) if token]
    if len(tokens) < 3:
        return title, None

    suffix_tokens = None
    if len(tokens) >= 2 and " ".join(tokens[-2:]).casefold() in {"radio edit", "extended mix"}:
        suffix_tokens = tokens[-2:]
        body_tokens = tokens[:-2]
    elif tokens[-1].casefold() in {"remix", "dub", "edit", "mix", "rework", "version", "remaster", "bootleg", "re-edit"}:
        suffix_tokens = [tokens[-1]]
        body_tokens = tokens[:-1]
    else:
        return title, None

    match = rules.match_producer_tail(body_tokens)
    if not match:
        return title, None

    producer, count = match
    title_tokens = body_tokens[:-count]
    if not title_tokens:
        return title, None
    mix_annotation = f"{producer} {' '.join(suffix_tokens)}"
    return " ".join(title_tokens), mix_annotation


def _apply_known_artist_anchor(stem: str, rules: "_learned.LearnedRules | None") -> tuple[str | None, str | None]:
    if not rules:
        return None, None
    tokens = [token for token in re.split(r"[\s_]+", stem) if token]
    match = rules.match_artist_prefix(tokens)
    if not match:
        return None, None
    artist, token_count = match
    remainder = " ".join(tokens[token_count:]).strip()
    return artist, remainder or None


def _is_unresolved_candidate(artist: str | None, title: str | None) -> bool:
    if not artist or not title:
        return True
    if _looks_like_junk_artist(artist):
        return True
    if _looks_like_junk_title(title):
        return True
    if _sanitize_filename(artist or "") == "Unknown":
        return True
    if _sanitize_filename(title or "") == "Unknown":
        return True
    return False


def _infer_artists_by_label(files: list[Path]) -> dict[str, str]:
    groups: dict[str, dict[str, int]] = {}
    for path in files:
        stem = _strip_release_junk(path.stem)
        code = _label_code(stem)
        if not code:
            continue
        rest = stem[len(code):].lstrip(" -_:")
        if " - " not in rest:
            continue
        left, _right = rest.split(" - ", 1)
        artist = _normalize_artist_text(left)
        if not artist or _looks_like_junk_artist(artist):
            continue
        groups.setdefault(code, {})[artist] = groups.setdefault(code, {}).get(artist, 0) + 1

    hints: dict[str, str] = {}
    for code, counts in groups.items():
        if not counts:
            continue
        artist, count = max(counts.items(), key=lambda item: item[1])
        if count >= 2:
            hints[code] = artist
    return hints


def _score_ambiguity(
    path: Path,
    metadata,
    artist: str | None,
    title: str | None,
    mix_annotation: str | None,
    label_artist_hints: dict[str, str] | None,
) -> tuple[int, list[str]]:
    reasons: list[str] = []
    score = 0

    tag_artist = metadata.artist
    tag_title = metadata.title

    if _looks_like_junk_artist(tag_artist):
        score += 3
        reasons.append("junk artist tag")
    if _looks_like_junk_title(tag_title):
        score += 2
        reasons.append("junk title tag")

    stem = _COPY_SUFFIX.sub("", path.stem).strip()
    has_label = _label_code(stem) is not None
    has_dash = " - " in stem
    stem_peeled = _strip_release_junk(stem)
    stem_peeled, _ = _extract_mix_annotation(stem_peeled)
    token_count = len([token for token in re.split(r"[\s_]+", stem_peeled) if token])

    if not has_label and not has_dash and mix_annotation is None:
        score += 2
        reasons.append("no structural hints in filename")

    if token_count > 5:
        score += 1
        reasons.append(f"{token_count} tokens after cleaning")

    if has_label:
        code = _label_code(stem)
        if code and (not label_artist_hints or code not in label_artist_hints):
            score += 1
            reasons.append(f"solo file for label {code}")

    if not artist:
        score += 2
        reasons.append("could not resolve artist")
    if not title:
        score += 2
        reasons.append("could not resolve title")

    return score, reasons


def probe_ambiguous(
    root: Path,
    *,
    top_n: int = 5,
    sample_size: int = 100,
    rules: "_learned.LearnedRules | None" = None,
) -> list[ProbeCandidate]:
    if rules is None:
        rules = _learned.load()

    files = _walk_audio_files(root)
    if not files:
        return []

    label_artist_hints = _infer_artists_by_label(files)
    sample = _stratified_sample(files, sample_size)
    candidates: list[ProbeCandidate] = []

    for file_path in sample:
        if rules.is_quarantined(file_path):
            continue
        if rules.lookup_manual(file_path) is not None:
            continue
        try:
            metadata = extract_metadata(file_path)
            artist, title, copy_suffix = _extract_artist_title(
                file_path,
                metadata,
                label_artist_hints=label_artist_hints,
                rules=rules,
            )
        except Exception as exc:
            log.debug("probe: metadata extraction failed for %s: %s", file_path, exc)
            continue

        clean_title, mix_annotation = _extract_mix_annotation(title or "")
        if clean_title:
            title = clean_title
        score, reasons = _score_ambiguity(
            file_path,
            metadata,
            artist,
            title,
            mix_annotation,
            label_artist_hints,
        )
        proposed = _generate_filename(artist, title, file_path.suffix, copy_suffix)
        if mix_annotation:
            proposed = _generate_filename(artist, f"{title} ({mix_annotation})", file_path.suffix, copy_suffix)

        candidates.append(ProbeCandidate(
            source_path=file_path,
            proposed_artist=artist,
            proposed_title=title,
            proposed_mix=mix_annotation,
            proposed_copy_suffix=copy_suffix,
            proposed_filename=proposed,
            score=score,
            reasons=reasons,
        ))

    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[:top_n]


def _extract_artist_title(
    path: Path,
    metadata,
    label_artist_hints: dict[str, str] | None = None,
    rules: "_learned.LearnedRules | None" = None,
) -> tuple[str | None, str | None, str | None]:
    """
    Best-effort extraction of artist, title, and copy suffix from metadata.
    Prefers tag fields, falls back to filename parsing.
    
    Artist priority: vocal artist (TPE1) > album artist (TPE2) > fallback
    Title: extracted from tags or filename, preserves remix/version markers.
    Copy suffix: extracted from original filename (e.g., "(2)", "(copy)", "(v2)")
    Removes only filler: PN suffixes, numeric prefixes/suffixes, underscores.
    
    Returns: (artist, title, copy_suffix) where copy_suffix is None or a string like "(2)"
    """
    # Try to get artist from tags with priority (vocalist > band > producer)
    artist = _get_prioritized_artist(path)
    
    # Fallback to scanner's metadata.artist if prioritized method returned nothing
    if not artist:
        artist = metadata.artist or None
    artist = _normalize_artist_text(artist)
    if artist and rules:
        artist = rules.canonical_artist(artist) or artist
    if _looks_like_junk_artist(artist):
        artist = None

    title = metadata.title or None
    if title:
        title = _strip_leading_key_bpm_prefix(str(title))
    copy_suffix = None
    
    # Extract copy suffix from original filename first (before any cleaning)
    stem_original = path.stem
    copy_match = _COPY_SUFFIX.search(stem_original)
    if copy_match:
        copy_suffix = f"({copy_match.group(1)})"
        # Remove copy suffix from stem for further processing
        stem_original = _COPY_SUFFIX.sub('', stem_original).strip()
    
    # Both found in tags — use them (preserves remix/dub markers if in tag)
    if artist and title:
        return artist, title, copy_suffix
    
    # Try filename-based fallback for title
    stem = stem_original

    # Strip Pioneer/MiX markers: _PN, _PN2, _PN 3, or PN (no underscore)
    stem = _PN_SUFFIX.sub('', stem).strip()

    # Strip numeric prefixes: "918223_Title" or "918223-Title"
    stem = _ID_PREFIX.sub('', stem).strip()

    # Strip numeric suffixes: "Title_918223" or "Title-918223"
    stem = _ID_SUFFIX.sub('', stem).strip()

    # Remove key/BPM leader chunks before artist-title parsing.
    stem = _strip_leading_key_bpm_prefix(stem)

    anchored_title = None

    if not artist and rules:
        anchored_artist, anchored_title = _apply_known_artist_anchor(stem, rules)
        if anchored_artist:
            artist = anchored_artist
            if anchored_title and not title:
                title = anchored_title

    # Parse "Artist - Title" from filename before underscore replacement.
    if ' - ' in stem and (not artist or not title):
        parts = stem.split(' - ', 1)
        if len(parts) == 2:
            left = parts[0].strip()
            right = parts[1].strip()
            if not artist and not _looks_like_junk_artist(left):
                artist = _normalize_artist_text(left)
                if artist and rules:
                    artist = rules.canonical_artist(artist) or artist
            if not title:
                title = right

    # Underscore fallback: "artist_title_version".
    if (not artist or not title) and '_' in stem:
        u = [p for p in stem.split('_') if p]
        if len(u) >= 2:
            if not artist and not _looks_like_junk_artist(u[0]):
                artist = _normalize_artist_text(u[0])
                if artist and rules:
                    artist = rules.canonical_artist(artist) or artist
            if not title:
                title = " ".join(u[1:]).strip()

    # Replace remaining underscores with spaces
    stem = _UNDERSCORE.sub(' ', stem).strip()

    # Last resort: use cleaned stem as title if we still have nothing
    if not title:
        title = stem if stem else None

    clean_title, mix_annotation = _extract_mix_annotation(title or "")
    if clean_title:
        title = clean_title
    if not mix_annotation:
        title, mix_annotation = _extract_known_producer_tail(title, rules)
    if mix_annotation:
        mix_annotation = _canonicalize_mix_annotation(mix_annotation, rules)
        title = f"{title} ({mix_annotation})"
    
    return artist or None, title or None, copy_suffix


def _sanitize_filename(text: str, max_len: int = 200) -> str:
    """
    Clean a string for use in a filename.
    Removes filesystem-unsafe characters, collapses spaces, strips dots.
    """
    text = _UNSAFE_CHARS.sub(" ", text)
    text = _MULTI_SPACE.sub(" ", text).strip().strip(".")
    return text[:max_len] if text else "Unknown"


def _generate_filename(artist: str | None, title: str | None, ext: str, copy_suffix: str | None = None) -> str:
    """
    Generate a clean filename with artist, title, and optional copy suffix.
    Format: "Artist: Title.ext" or "Artist: Title (2).ext"
    
    Artist and title are both included in filename for clear visual identification.
    Copy suffix (e.g., "(2)", "(copy)") is appended before the extension if present.
    Full metadata remains in ID3 tags for database searchability.
    """
    artist = _sanitize_filename(artist or "Unknown")
    title = _sanitize_filename(title or "Unknown")
    title = _strip_leading_artist_from_title(artist, title)
    suffix_str = f" {copy_suffix}" if copy_suffix else ""
    return f"{artist}: {title}{suffix_str}{ext}"


def _resolve_filename_collision(dest: Path) -> Path:
    """
    If dest already exists, append (2), (3), ... until a free slot is found.
    Returns the new collision-safe path or None if no slot found within 100 attempts.
    
    Uses (2), (3) format to match standard copy naming conventions.
    """
    if not dest.exists():
        return dest
    
    stem, suffix = dest.stem, dest.suffix
    for i in range(2, 101):
        candidate = dest.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    
    return None  # No free slot found (extremely unlikely)


def _no_name_dir(library_root: Path) -> Path:
    return library_root.resolve().parent / _NO_NAME_FOLDER


def _append_quarantine_manifest(manifest_path: Path, entry: dict) -> None:
    payload = {"version": 1, "files": []}
    if manifest_path.exists():
        try:
            raw = json.loads(manifest_path.read_text())
            if isinstance(raw, dict):
                payload.update(raw)
        except (OSError, json.JSONDecodeError):
            log.warning("Could not read quarantine manifest %s — recreating", manifest_path)

    files = payload.get("files")
    if not isinstance(files, list):
        files = []
    files.append(entry)
    payload["files"] = files
    manifest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def quarantine_track(source_path: Path, library_root: Path) -> dict:
    if not source_path.exists():
        raise FileNotFoundError(f"Source file not found: {source_path}")

    quarantine_dir = _no_name_dir(library_root)
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    dest_path = _resolve_filename_collision(quarantine_dir / source_path.name)
    if dest_path is None:
        raise RuntimeError(f"Could not create a unique destination for {source_path.name}")

    source_path.rename(dest_path)
    manifest_path = quarantine_dir / _NO_NAME_MANIFEST
    _append_quarantine_manifest(manifest_path, {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "original_path": str(source_path),
        "quarantined_path": str(dest_path),
        "library_root": str(library_root),
    })

    return {
        "quarantine_dir": str(quarantine_dir),
        "manifest_path": str(manifest_path),
        "dest_path": str(dest_path),
        "dest_name": dest_path.name,
    }


def _walk_audio_files(root: Path) -> list[Path]:
    """Return all audio files under root, respecting skip lists."""
    files: list[Path] = []
    try:
        for dirpath, dirnames, filenames in root.walk():
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIRS and not d.startswith(".")
            ]
            for filename in filenames:
                if any(filename.startswith(p) for p in SKIP_PREFIXES):
                    continue
                file_path = dirpath / filename
                if file_path.suffix.lower() in AUDIO_EXTENSIONS:
                    files.append(file_path)
    except OSError as e:
        log.warning(f"Error walking {root}: {e}")
    return files


def _rename_one(
    path: Path,
    db=None,
    dry_run: bool = True,
    rules: "_learned.LearnedRules | None" = None,
    label_artist_hints: dict[str, str] | None = None,
    library_root: Path | None = None,
) -> RenameResult:
    """
    Rename a single audio file based on its metadata.
    Updates rekordbox DjmdContent.FolderPath if db is provided.
    
    Returns: RenameResult with action and outcome.
    """
    try:
        if rules and rules.is_quarantined(path):
            return RenameResult(
                original_path=path,
                new_path=path,
                action="no_change",
                reason="Path is quarantined in learned rules",
            )

        manual_name = rules.lookup_manual(path) if rules else None
        metadata = extract_metadata(path)
        artist, title, copy_suffix = _extract_artist_title(
            path,
            metadata,
            label_artist_hints=label_artist_hints,
            rules=rules,
        )
    except Exception as e:
        return RenameResult(
            original_path=path,
            new_path=None,
            action="error",
            reason=f"Metadata extraction failed: {e}",
        )
    
    ext = path.suffix
    if manual_name:
        new_name = manual_name if Path(manual_name).suffix else f"{manual_name}{ext}"
    else:
        if not manual_name and _is_unresolved_candidate(artist, title):
            target_root = library_root or path.parent
            if dry_run:
                return RenameResult(
                    original_path=path,
                    new_path=_no_name_dir(target_root) / path.name,
                    action="quarantined",
                    reason="Would move unresolved file to No-Name tracks for Tagging",
                )
            moved = quarantine_track(path, target_root)
            if db is not None:
                try:
                    _update_db_path(path, Path(moved["dest_path"]), db)
                except Exception as exc:
                    log.warning("Database update failed for quarantined file %s: %s", path, exc)
            return RenameResult(
                original_path=path,
                new_path=Path(moved["dest_path"]),
                action="quarantined",
                reason="Moved unresolved file to No-Name tracks for Tagging",
            )
        new_name = _generate_filename(artist, title, ext, copy_suffix)
    new_path = path.parent / new_name
    
    # If the new name matches the current name, skip
    if new_path == path:
        return RenameResult(
            original_path=path,
            new_path=path,
            action="no_change",
            reason="Filename already matches metadata",
        )
    
    # Handle collisions
    if new_path.exists():
        collision_path = _resolve_filename_collision(new_path)
        if collision_path is None:
            return RenameResult(
                original_path=path,
                new_path=None,
                action="error",
                reason="No available collision-free slot",
            )
        new_path = collision_path
        action = "collision_numbered"
    else:
        action = "renamed"
    
    if not dry_run:
        try:
            path.rename(new_path)
            log.info(f"Renamed: {path.name} → {new_path.name}")
            
            # Update rekordbox if db is provided
            if db is not None:
                try:
                    _update_db_path(path, new_path, db)
                except Exception as e:
                    log.warning(f"Database update failed for {new_path}: {e}")
        except OSError as e:
            return RenameResult(
                original_path=path,
                new_path=None,
                action="error",
                reason=f"Rename failed: {e}",
            )
    
    return RenameResult(
        original_path=path,
        new_path=new_path,
        action=action,
    )


def _update_db_path(old_path: Path, new_path: Path, db) -> None:
    """
    Update rekordbox DjmdContent.FolderPath for the given file.
    Matches by file hash (same strategy as relocator.py).
    """
    if not hasattr(db, 'update_content_path'):
        log.debug("Database does not support update_content_path — skipping DB update")
        return
    
    # Search for content row with matching file
    try:
        content_row = db.search_by_path(str(old_path))
        if content_row:
            db.update_content_path(content_row, new_path, check_path=True)
            log.debug(f"Updated DB: {old_path.name} → {new_path.name}")
    except Exception as e:
        log.warning(f"Database lookup/update failed: {e}")


# ─── Public interface ────────────────────────────────────────────────────────

def rename_directory(
    root: Path,
    db=None,
    *,
    dry_run: bool = True,
    max_workers: int = 1,
    rules: "_learned.LearnedRules | None" = None,
) -> list[RenameResult]:
    """
    Batch-rename all audio files in a directory based on their metadata.
    
    Parameters
    ----------
    root : Path
        Directory to scan for audio files.
    db : Rekordbox6Database, optional
        If provided, updates DjmdContent.FolderPath for each renamed file.
    dry_run : bool
        If True (default), compute and report changes without touching files.
        Pass dry_run=False to execute renames.
    max_workers : int
        Parallel workers for rename operations (default 1 = sequential).
    
    Returns
    -------
    list[RenameResult]
        Outcome for each file processed.
    """
    if rules is None:
        rules = _learned.load()

    files = _walk_audio_files(root)
    total = len(files)
    results: list[RenameResult] = []
    label_artist_hints = _infer_artists_by_label(files)
    
    if total == 0:
        log.info(f"No audio files found in {root}")
        return results
    
    log.info(
        f"Renaming {total} files in {root}  dry_run={dry_run}  workers={max_workers}"
    )
    
    renamed = skipped = collisions = errors = quarantined = 0
    
    def _emit() -> None:
        print(
            "REKITBOX_PROGRESS: " + json.dumps({
                "done":      len(results),
                "total":     total,
                "remaining": total - len(results),
                "renamed":   renamed,
                "skipped":   skipped,
                "collisions": collisions,
                "quarantined": quarantined,
                "errors":    errors,
            }),
            flush=True,
        )
    
    for i, file_path in enumerate(files):
        result = _rename_one(
            file_path,
            db=db,
            dry_run=dry_run,
            rules=rules,
            label_artist_hints=label_artist_hints,
            library_root=root,
        )
        results.append(result)
        
        if result.action == "renamed":
            renamed += 1
        elif result.action == "no_change":
            skipped += 1
        elif result.action == "collision_numbered":
            collisions += 1
        elif result.action == "quarantined":
            quarantined += 1
        elif result.action == "error":
            errors += 1
        
        if (i + 1) % max(1, total // 20) == 0 or i == total - 1:
            _emit()
    
    log.info(
        f"Rename complete: {renamed} renamed, {skipped} skipped, "
        f"{collisions} collisions handled, {quarantined} quarantined, {errors} errors"
    )
    
    return results
