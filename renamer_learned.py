from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

STORE_PATH = Path.home() / ".rekordbox-toolkit" / "renamer_learned.json"
SCHEMA_VERSION = 1


def _canon_artist(text: str) -> str:
    text = text.casefold().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _looks_like_real_name(text: str) -> bool:
    text = text.strip()
    if len(text) < 2:
        return False
    if re.fullmatch(r"\d+", text):
        return False
    if re.fullmatch(r"[A-Za-z]{2,6}\d{2,5}", text):
        return False
    if re.match(r"^\s*unknown(\s|$)", text, re.IGNORECASE):
        return False
    return True


def _extract_mix_producer(mix: str | None) -> str | None:
    if not mix:
        return None
    producer = mix.strip()
    producer = re.sub(
        r"\s+(remix|dub|edit|mix|rework|version|remaster|bootleg|re-edit|radio\s+edit|extended\s+mix)\s*$",
        "",
        producer,
        flags=re.IGNORECASE,
    ).strip(" -_()")
    if not producer:
        return None
    return producer


@dataclass
class LearnedRules:
    manual_renames: dict[str, str] = field(default_factory=dict)
    producer_aliases: dict[str, str] = field(default_factory=dict)
    known_artists: list[str] = field(default_factory=list)
    known_producers: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)

    _artists_index: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _producers_index: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._rebuild_indexes()

    def _rebuild_indexes(self) -> None:
        self._artists_index = {_canon_artist(value): value for value in self.known_artists if value}
        self._producers_index = {_canon_artist(value): value for value in self.known_producers if value}

    def lookup_manual(self, source_path: Path | str) -> str | None:
        return self.manual_renames.get(str(source_path))

    def canonical_artist(self, candidate: str | None) -> str | None:
        if not candidate:
            return None
        return self._artists_index.get(_canon_artist(candidate))

    def canonical_producer(self, candidate: str | None) -> str | None:
        if not candidate:
            return None
        return self._producers_index.get(_canon_artist(candidate))

    def producer_alias(self, lowered_token: str) -> str | None:
        return self.producer_aliases.get(lowered_token.lower())

    def is_quarantined(self, source_path: Path | str) -> bool:
        return str(source_path) in set(self.quarantined)

    def match_artist_prefix(self, tokens: list[str]) -> tuple[str, int] | None:
        if not tokens or not self._artists_index:
            return None
        lowered = [token.lower() for token in tokens]
        best: tuple[str, int] | None = None
        for canon_key, canonical in sorted(self._artists_index.items(), key=lambda item: -len(item[0].split())):
            name_tokens = canon_key.split()
            count = len(name_tokens)
            if count > len(lowered):
                continue
            if lowered[:count] == name_tokens:
                if best is None or count > best[1]:
                    best = (canonical, count)
                break
        return best

    def match_producer_tail(self, tokens: list[str]) -> tuple[str, int] | None:
        if not tokens or not self._producers_index:
            return None
        lowered = [token.lower() for token in tokens]
        for canon_key, canonical in sorted(self._producers_index.items(), key=lambda item: -len(item[0].split())):
            name_tokens = canon_key.split()
            count = len(name_tokens)
            if count > len(lowered):
                continue
            if lowered[-count:] == name_tokens:
                return (canonical, count)
        return None

    def add_manual_rename(self, source_path: Path | str, target_name: str) -> None:
        self.manual_renames[str(source_path)] = target_name
        self._log("manual_rename", str(source_path), target_name)

    def add_producer_alias(self, lowered: str, canonical: str) -> None:
        self.producer_aliases[lowered.lower()] = canonical
        self._log("producer_alias", lowered.lower(), canonical)

    def add_known_artist(self, name: str) -> None:
        name = name.strip()
        if not name:
            return
        canon = _canon_artist(name)
        if canon in self._artists_index:
            return
        self.known_artists.append(name)
        self._artists_index[canon] = name
        self._log("known_artist", canon, name)

    def add_known_producer(self, name: str) -> None:
        name = name.strip()
        if not name:
            return
        canon = _canon_artist(name)
        if canon in self._producers_index:
            return
        self.known_producers.append(name)
        self._producers_index[canon] = name
        self._log("known_producer", canon, name)

    def add_quarantine(self, source_path: Path | str) -> None:
        source = str(source_path)
        if source not in self.quarantined:
            self.quarantined.append(source)
            self._log("quarantine", source, "")

    def retract(self, rule_type: str, key: str) -> bool:
        removed = False
        if rule_type == "manual_rename":
            removed = self.manual_renames.pop(key, None) is not None
        elif rule_type == "producer_alias":
            removed = self.producer_aliases.pop(key.lower(), None) is not None
        elif rule_type == "known_artist":
            canon = _canon_artist(key)
            if canon in self._artists_index:
                canonical = self._artists_index.pop(canon)
                self.known_artists = [value for value in self.known_artists if value != canonical]
                removed = True
        elif rule_type == "known_producer":
            canon = _canon_artist(key)
            if canon in self._producers_index:
                canonical = self._producers_index.pop(canon)
                self.known_producers = [value for value in self.known_producers if value != canonical]
                removed = True
        elif rule_type == "quarantine":
            before = len(self.quarantined)
            self.quarantined = [value for value in self.quarantined if value != key]
            removed = len(self.quarantined) < before
        if removed:
            self._log("retract", f"{rule_type}:{key}", "")
        return removed

    def _log(self, rule: str, key: str, value: str) -> None:
        self.history.append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "rule": rule,
            "key": key,
            "value": value,
        })

    def to_dict(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "manual_renames": self.manual_renames,
            "producer_aliases": self.producer_aliases,
            "known_artists": self.known_artists,
            "known_producers": self.known_producers,
            "quarantined": self.quarantined,
            "history": self.history,
        }


def load(path: Path = STORE_PATH) -> LearnedRules:
    try:
        if not path.exists():
            return LearnedRules()
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read %s: %s — starting with empty store", path, exc)
        return LearnedRules()

    if not isinstance(raw, dict):
        log.warning("%s is not a JSON object — ignoring", path)
        return LearnedRules()

    version = raw.get("version", 1)
    if version != SCHEMA_VERSION:
        log.warning(
            "renamer_learned.json has version %s, this build expects %s — loading best-effort",
            version,
            SCHEMA_VERSION,
        )

    return LearnedRules(
        manual_renames=dict(raw.get("manual_renames") or {}),
        producer_aliases=dict(raw.get("producer_aliases") or {}),
        known_artists=list(raw.get("known_artists") or []),
        known_producers=list(raw.get("known_producers") or []),
        quarantined=list(raw.get("quarantined") or []),
        history=list(raw.get("history") or []),
    )


def save(rules: LearnedRules, path: Path = STORE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(rules.to_dict(), indent=2, ensure_ascii=False))
    tmp.replace(path)


def parse_confirmed_filename(name: str) -> tuple[str | None, str | None, str | None]:
    stem = Path(name).stem
    if ":" not in stem:
        return None, None, None
    artist_part, rest = stem.split(":", 1)
    artist = artist_part.strip()

    mix_annotation = None
    match = re.search(r"\s*\(([^()]+)\)\s*$", rest)
    if match:
        inside = match.group(1).strip()
        if not re.fullmatch(r"\d+|copy|duplicate|v\d+", inside, flags=re.IGNORECASE):
            mix_annotation = inside
            rest = rest[:match.start()]
    title = rest.strip()
    return artist or None, title or None, mix_annotation


def harvest_from_confirmation(rules: LearnedRules, confirmed_name: str) -> None:
    artist, _title, mix = parse_confirmed_filename(confirmed_name)
    if artist and _looks_like_real_name(artist):
        rules.add_known_artist(artist)

    if not mix:
        return

    producer = mix.split(":", 1)[0].strip() if ":" in mix else _extract_mix_producer(mix)
    if producer and _looks_like_real_name(producer):
        rules.add_known_producer(producer)