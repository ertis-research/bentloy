"""Deploy failure reporting: a short reason for `error`, the full log for `error_log`."""

import re

MAX_ERROR_CHARS = 500

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_ERROR_LINE_RE = re.compile(
    r"error|failed|unauthorized|denied|forbidden|not found|exception|no such",
    re.IGNORECASE,
)
_PUSH_DENIED_RE = re.compile(r"unauthorized|denied|push permission", re.IGNORECASE)
_REGISTRY_CODE_RE = re.compile(r"\b(UNAUTHORIZED|DENIED|FORBIDDEN|NAME_UNKNOWN)\b.*")


class DeployStepError(RuntimeError):
    """A deploy step failure with a short, user-facing message and an optional full log."""

    def __init__(self, message: str, log: str | None = None):
        super().__init__(message)
        self.message = message
        self.log = log or None


def truncate(text: str, limit: int = MAX_ERROR_CHARS) -> str:
    """Trim text to `limit` characters, marking the cut."""

    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _clean_lines(log: str) -> list[str]:
    return [line.strip() for line in _ANSI_RE.sub("", log or "").splitlines() if line.strip()]


def extract_failure_reason(log: str) -> str:
    """Return the most relevant error line of a log (last error-looking line, else last line)."""

    lines = _clean_lines(log)
    if not lines:
        return ""
    for line in reversed(lines):
        if line.upper().startswith("INFO"):
            continue
        if _ERROR_LINE_RE.search(line):
            return line
    return lines[-1]


def build_failure(
    prepare_logs: str,
    kaniko_logs: str,
    target_image: str,
    registry_secret: str,
) -> DeployStepError:
    """Summarize a failed build Job.

    `prepare-build-context` is an init container: when it fails, kaniko never runs,
    so the reason comes from kaniko's log when there is one, else from prepare's.
    """

    if _clean_lines(kaniko_logs):
        step, reason = "kaniko", extract_failure_reason(kaniko_logs)
    elif _clean_lines(prepare_logs):
        step, reason = "prepare-build-context", extract_failure_reason(prepare_logs)
    else:
        step, reason = "", ""

    if not reason:
        message = "Build Job failed (no logs available)."
    elif step == "kaniko" and _PUSH_DENIED_RE.search(reason):
        # Kaniko puts the registry's answer (UNAUTHORIZED: ...) at the end of a long line.
        registry_answer = _REGISTRY_CODE_RE.search(reason)
        message = (
            f"Registry push denied for {target_image}: check that secret "
            f"'{registry_secret}' has push access to that repository. "
            f"[{step}] {registry_answer.group(0) if registry_answer else reason}"
        )
    else:
        message = f"Build Job failed [{step}]: {reason}"

    full_log = (
        f"[prepare-build-context]\n{prepare_logs or ''}\n\n[kaniko]\n{kaniko_logs or ''}"
    )
    return DeployStepError(truncate(message), log=full_log)
