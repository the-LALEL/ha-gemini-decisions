"""Translate between Home Assistant evaluation tasks and the Decisions API.

Everything here is plain Python with no I/O except `build_body`, which reads
attachment files and must run in the executor.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import math
import mimetypes
from pathlib import Path
from typing import Any
import unicodedata

from homeassistant.components import ai_task
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.json import json_bytes, json_dumps

from . import const


@dataclass(slots=True)
class WireQuestion:
    """One question as sent, plus how to map its answer back."""

    original_id: str
    question_type: str
    payload: dict[str, Any]
    # choice: wire option key -> original option key, in HA's order
    options: dict[str, str] = field(default_factory=dict)
    levels: int = 0


def _clean_option_key(key: str) -> str:
    """Make an option key Google accepts, keeping it readable."""
    chars = []
    for char in key:
        if unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp"):
            chars.append(" ")
        elif char.isspace() and char != " ":
            chars.append(" ")
        else:
            chars.append(char)
    cleaned = "".join(chars).strip()
    cleaned = cleaned[: const.MAX_OPTION_CHARS]
    while len(cleaned.encode()) > const.MAX_OPTION_BYTES:
        cleaned = cleaned[:-1]
    return cleaned.rstrip()


def build_questions(
    questions: dict[str, ai_task.EvaluationQuestion],
) -> list[WireQuestion]:
    """Map HA questions to wire questions with safe positional keys."""
    if len(questions) > const.MAX_QUESTIONS:
        raise ServiceValidationError(
            f"Gemini Decisions accepts at most {const.MAX_QUESTIONS} questions per call"
        )
    wire: list[WireQuestion] = []
    for question_id, question in questions.items():
        instructions = question["instructions"]
        if not instructions.strip():
            raise ServiceValidationError(f"Question {question_id} has empty instructions")
        qtype = question["type"]
        if qtype == "noul":
            wire.append(
                WireQuestion(
                    question_id,
                    "noul",
                    {"type": "noul", "instructions": instructions},
                )
            )
        elif qtype == "choice":
            criteria: dict[str, str] = question["criteria"]
            if len(criteria) > const.MAX_OPTIONS:
                raise ServiceValidationError(
                    f"Question {question_id} has more than {const.MAX_OPTIONS} options"
                )
            options: dict[str, str] = {}
            wire_criteria: dict[str, str] = {}
            for index, (option, description) in enumerate(criteria.items()):
                key = _clean_option_key(option) or f"option_{index}"
                if key in wire_criteria:
                    base, n = key, 2
                    while key in wire_criteria:
                        suffix = f" ({n})"
                        key = base[: const.MAX_OPTION_CHARS - len(suffix)] + suffix
                        n += 1
                if key != option:
                    # The model reads option keys, so keep the original visible.
                    description = f'Option "{option}". {description}'
                options[key] = option
                wire_criteria[key] = description
            wire.append(
                WireQuestion(
                    question_id,
                    "choice",
                    {
                        "type": "choice",
                        "instructions": instructions,
                        "criteria": wire_criteria,
                    },
                    options=options,
                )
            )
        elif qtype == "score":
            levels: list[str] = question["criteria"]
            if len(levels) > const.MAX_OPTIONS:
                raise ServiceValidationError(
                    f"Question {question_id} has more than {const.MAX_OPTIONS} levels"
                )
            wire.append(
                WireQuestion(
                    question_id,
                    "score",
                    {"type": "score", "instructions": instructions, "criteria": list(levels)},
                    levels=len(levels),
                )
            )
        else:
            raise ServiceValidationError(f"Question {question_id} has unknown type {qtype}")
    return wire


def state_text(state: Any) -> str | None:
    """Return the text form of the state, or None when there is none."""
    if state is None:
        return None
    if isinstance(state, str):
        text = state
    else:
        try:
            text = json_dumps(state)
        except (TypeError, ValueError) as err:
            raise ServiceValidationError(
                "The state passed to evaluate can't be turned into JSON"
            ) from err
    return text if text.strip() else None


_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"BM", "image/bmp"),
)


def _sniff(head: bytes) -> str:
    for magic, mime in _MAGIC:
        if head.startswith(magic):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    return ""


def normalize_mime(declared: str | None, path: Path, head: bytes) -> str:
    """Return a MIME type the API accepts, or raise."""
    mime = (declared or "").partition(";")[0].strip().lower()
    mime = const.MIME_ALIASES.get(mime, mime)
    if mime in const.GENERIC_TYPES:
        mime = _sniff(head)
        if not mime:
            guessed = (mimetypes.guess_type(path.name)[0] or "").lower()
            mime = const.MIME_ALIASES.get(guessed, guessed)
    if mime in (
        const.IMAGE_TYPES | const.AUDIO_TYPES | const.VIDEO_TYPES | const.DOCUMENT_TYPES
    ):
        return mime
    raise ServiceValidationError(
        f"Gemini Decisions can't send {mime or 'this file type'}; it accepts common "
        "image, audio and video types and PDF"
    )


def _block_type(mime: str) -> str:
    if mime in const.IMAGE_TYPES:
        return "image"
    if mime in const.AUDIO_TYPES:
        return "audio"
    if mime in const.VIDEO_TYPES:
        return "video"
    return "document"


def build_body(
    model: str,
    text: str | None,
    attachments: list[tuple[str, str | None, Path]],
    wire: list[WireQuestion],
    resolution: str,
    max_encoded_bytes: int,
) -> bytes:
    """Build the JSON request body. Reads files, so run it in the executor.

    attachments: (media_content_id, declared_mime, path).
    """
    if not text and not attachments:
        raise ServiceValidationError("There is nothing to evaluate: state is empty")

    # Check sizes before reading anything.
    total = 0
    sizes: list[int] = []
    for media_id, _, path in attachments:
        try:
            size = path.stat().st_size
        except OSError as err:
            raise HomeAssistantError(f"Couldn't read attachment {media_id}") from err
        sizes.append(size)
        total += (size + 2) // 3 * 4
    if total > max_encoded_bytes:
        raise ServiceValidationError(
            f"Attachments are too large: {total // (1024 * 1024)} MB after encoding, "
            f"limit {max_encoded_bytes // (1024 * 1024)} MB"
        )

    questions = {f"q{index}": item.payload for index, item in enumerate(wire)}
    if not attachments:
        payload: dict[str, Any] = {"model": model, "input": text, "questions": questions}
        return json_bytes(payload)

    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for (media_id, declared, path), size in zip(attachments, sizes, strict=True):
        try:
            data = path.read_bytes()
        except OSError as err:
            raise HomeAssistantError(f"Couldn't read attachment {media_id}") from err
        mime = normalize_mime(declared, path, data[:16])
        if mime in const.DOCUMENT_TYPES and size > const.HARD_MAX_PDF_BYTES:
            raise ServiceValidationError("PDF attachments must be 50 MB or smaller")
        kind = _block_type(mime)
        block: dict[str, Any] = {
            "type": kind,
            "data": base64.b64encode(data).decode("ascii"),
            "mime_type": mime,
        }
        if kind in ("image", "video"):
            block["resolution"] = (
                "high" if kind == "video" and resolution == "ultra_high" else resolution
            )
        blocks.append(block)
        del data
    return json_bytes({"model": model, "input": blocks, "questions": questions})


def _probability(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HomeAssistantError(f"Gemini returned a non-number for {where}")
    try:
        number = float(value)
    except OverflowError as err:
        raise HomeAssistantError(f"Gemini returned an out-of-range value for {where}") from err
    if not math.isfinite(number) or not -1e-6 <= number <= 1 + 1e-6:
        raise HomeAssistantError(f"Gemini returned an out-of-range value for {where}")
    return min(1.0, max(0.0, number))


def parse_answers(
    response: dict[str, Any], wire: list[WireQuestion], warn: Any
) -> ai_task.EvaluationTaskResult:
    """Validate the API reply and map it back to HA's IDs and order."""
    feedback = response.get("prompt_feedback")
    if isinstance(feedback, dict) and feedback.get("block_reason"):
        raise HomeAssistantError(f"Gemini blocked this input ({feedback['block_reason']})")
    answers = response.get("answers")
    if not isinstance(answers, dict) or not answers:
        raise HomeAssistantError("Gemini returned no answers")

    expected = {f"q{index}" for index in range(len(wire))}
    extra = set(answers) - expected
    if extra:
        warn("Gemini returned %d unexpected answers; ignoring them", len(extra))

    result: dict[str, ai_task.EvaluationAnswer] = {}
    for index, item in enumerate(wire):
        key = f"q{index}"
        answer = answers.get(key)
        name = item.original_id
        if not isinstance(answer, dict):
            raise HomeAssistantError(f"Gemini didn't answer question {name}")
        if answer.get("type") != item.question_type:
            raise HomeAssistantError(f"Gemini answered question {name} with the wrong type")

        if item.question_type == "noul":
            result[name] = ai_task.NoulAnswer(noul=_probability(answer.get("noul"), name))

        elif item.question_type == "choice":
            choice = answer.get("choice")
            raw = answer.get("probabilities")
            if not isinstance(choice, str) or choice not in item.options:
                raise HomeAssistantError(f"Gemini picked an unknown option for {name}")
            if not isinstance(raw, dict):
                raise HomeAssistantError(f"Gemini sent no probabilities for {name}")
            unknown = set(raw) - set(item.options)
            if unknown:
                raise HomeAssistantError(f"Gemini sent probabilities for unknown options in {name}")
            probabilities = {
                original: _probability(raw.get(wire_key, 0.0), name)
                for wire_key, original in item.options.items()
            }
            result[name] = ai_task.ChoiceAnswer(
                choice=item.options[choice], probabilities=probabilities
            )

        else:
            raw = answer.get("probabilities")
            if not isinstance(raw, dict):
                raise HomeAssistantError(f"Gemini sent no probabilities for {name}")
            levels = [0.0] * item.levels
            for level_key, value in raw.items():
                if not (
                    isinstance(level_key, str)
                    and 0 < len(level_key) <= 4
                    and level_key.isascii()
                    and level_key.isdigit()
                    and str(int(level_key)) == level_key
                    and int(level_key) < item.levels
                ):
                    raise HomeAssistantError(f"Gemini sent an unknown level for {name}")
                levels[int(level_key)] = _probability(value, name)
            score = answer.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise HomeAssistantError(f"Gemini sent no score for {name}")
            try:
                score = float(score)
            except OverflowError as err:
                raise HomeAssistantError(f"Gemini sent an out-of-range score for {name}") from err
            top = item.levels - 1
            if not math.isfinite(score) or not -1e-6 <= score <= top + 1e-6:
                raise HomeAssistantError(f"Gemini sent an out-of-range score for {name}")
            result[name] = ai_task.ScoreAnswer(
                score=min(float(top), max(0.0, score)), probabilities=levels
            )

    return ai_task.EvaluationTaskResult(answers=result)
