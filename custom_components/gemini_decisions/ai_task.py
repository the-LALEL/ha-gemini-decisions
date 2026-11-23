"""AI Task entity that answers evaluate tasks with the Gemini Decisions API."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, override

from homeassistant.components import ai_task
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import DecisionsConfigEntry
from .api import DecisionsAuthError, DecisionsError, DecisionsLimitReached
from .const import (
    CONF_ALLOW_ATTACHMENTS,
    CONF_MAX_ATTACHMENT_MB,
    CONF_MAX_PER_DAY,
    CONF_MAX_PER_MINUTE,
    CONF_MODEL,
    CONF_RESOLUTION,
    CONF_TIMEOUT,
    DEFAULT_ALLOW_ATTACHMENTS,
    DEFAULT_MAX_ATTACHMENT_MB,
    DEFAULT_MAX_PER_DAY,
    DEFAULT_MAX_PER_MINUTE,
    DEFAULT_MODEL,
    DEFAULT_RESOLUTION,
    DEFAULT_TIMEOUT,
    DEFAULT_TITLE,
    DOMAIN,
    HARD_MAX_ENCODED_BYTES,
    ISSUE_DAILY_CAP,
    LOGGER,
    MAX_ATTACHMENTS,
)
from .mapping import build_body, build_questions, parse_answers, state_text

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DecisionsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the single Decisions entity."""
    async_add_entities([GeminiDecisionsEntity(entry)])


class GeminiDecisionsEntity(ai_task.AITaskEntity):
    """Answers ai_task.evaluate with calibrated probabilities from Gemini."""

    _attr_has_entity_name = True
    _attr_name = None
    _unrecorded_attributes = frozenset(
        {"requests_today", "input_tokens_today", "last_success", "last_error"}
    )
    _attr_supported_features = (
        ai_task.AITaskEntityFeature.EVALUATE
        | ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS
    )

    def __init__(self, entry: DecisionsConfigEntry) -> None:
        """Set up the entity for one config entry."""
        self._entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=DEFAULT_TITLE,
            manufacturer="Google",
            model="Gemini Decision",
            entry_type=DeviceEntryType.SERVICE,
        )
        self._last_error: str | None = None
        self._last_success: str | None = None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Show usage and health without any request content."""
        usage = self._entry.runtime_data.limiter.usage
        current = usage.day == dt_util.now().date().isoformat()
        return {
            "requests_today": usage.requests if current else 0,
            "input_tokens_today": usage.tokens if current else 0,
            "last_success": self._last_success,
            "last_error": self._last_error,
        }

    def _option(self, key: str, default: Any) -> Any:
        return self._entry.options.get(key, default)

    def _save_usage(self) -> None:
        runtime = self._entry.runtime_data
        runtime.store.async_delay_save(lambda: asdict(runtime.limiter.usage), 10)

    @override
    async def _async_evaluate(
        self, task: ai_task.EvaluationTask
    ) -> ai_task.EvaluationTaskResult:
        """Send the questions to Gemini and map the answers back."""
        runtime = self._entry.runtime_data
        wire = build_questions(task.questions)
        text = state_text(task.state)

        attachments: list[tuple[str, str | None, Any]] = []
        if task.attachments:
            if not self._option(CONF_ALLOW_ATTACHMENTS, DEFAULT_ALLOW_ATTACHMENTS):
                raise ServiceValidationError(
                    "Attachments are turned off for Gemini Decisions. Turn them on in "
                    "its options; on Google's free tier, Google may use them to improve "
                    "its products"
                )
            if len(task.attachments) > MAX_ATTACHMENTS:
                raise ServiceValidationError(
                    f"Gemini Decisions accepts at most {MAX_ATTACHMENTS} attachments per call"
                )
            attachments = [
                (item.media_content_id, item.mime_type, item.path)
                for item in task.attachments
            ]
        if text is None and not attachments:
            raise ServiceValidationError("There is nothing to evaluate: state is empty")

        max_mb = int(self._option(CONF_MAX_ATTACHMENT_MB, DEFAULT_MAX_ATTACHMENT_MB))
        max_bytes = min(HARD_MAX_ENCODED_BYTES, max_mb * 1024 * 1024)
        user_id = self._context.user_id if self._context else None
        LOGGER.info(
            "Evaluate task %s (user %s): %d questions, %d attachments",
            task.name,
            user_id or "automation",
            len(wire),
            len(attachments),
        )

        self._apply_limit(runtime.limiter.check)
        async with runtime.limiter.semaphore:
            body = await self.hass.async_add_executor_job(
                build_body,
                str(self._option(CONF_MODEL, DEFAULT_MODEL)).removeprefix("models/"),
                text,
                attachments,
                wire,
                str(self._option(CONF_RESOLUTION, DEFAULT_RESOLUTION)),
                max_bytes,
            )
            self._apply_limit(runtime.limiter.reserve)
            self._save_usage()

            body_mb = len(body) / (1024 * 1024)
            budget = min(
                120.0,
                max(float(self._option(CONF_TIMEOUT, DEFAULT_TIMEOUT)), 10 + 3 * body_mb),
            )
            try:
                response = await runtime.client.async_create(body, budget)
            except DecisionsAuthError as err:
                self._entry.async_start_reauth(self.hass)
                self._record_error(str(err))
                raise HomeAssistantError(
                    "Google rejected the Gemini Decisions API key; re-enter it under Settings > Devices & services"
                ) from err
            except DecisionsError as err:
                self._record_error(str(err))
                raise HomeAssistantError(str(err)) from err
            finally:
                del body

        usage = response.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("total_input_tokens"), int):
            runtime.limiter.add_tokens(usage["total_input_tokens"])
            self._save_usage()
        LOGGER.debug("Decisions usage: %s", usage)

        try:
            result = parse_answers(response, wire, LOGGER.warning)
        except HomeAssistantError as err:
            self._record_error(str(err))
            raise

        self._last_error = None
        self._last_success = dt_util.utcnow().isoformat()
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_DAILY_CAP)
        self.async_write_ha_state()
        return result

    def _limits(self) -> tuple[int, int]:
        return (
            int(self._option(CONF_MAX_PER_MINUTE, DEFAULT_MAX_PER_MINUTE)),
            int(self._option(CONF_MAX_PER_DAY, DEFAULT_MAX_PER_DAY)),
        )

    def _apply_limit(self, method: Any) -> None:
        """Run limiter.check or limiter.reserve and surface a cap as an error."""
        per_minute, per_day = self._limits()
        try:
            method(dt_util.now().date(), per_minute, per_day)
        except DecisionsLimitReached as err:
            if "daily cap" in str(err):
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    ISSUE_DAILY_CAP,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=ISSUE_DAILY_CAP,
                    translation_placeholders={"cap": str(per_day)},
                )
            self._record_error(str(err))
            raise HomeAssistantError(str(err)) from err

    def _record_error(self, message: str) -> None:
        self._last_error = message
        self.async_write_ha_state()
