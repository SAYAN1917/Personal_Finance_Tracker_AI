"""Application configuration.

All secrets come from environment variables - never from code or config files
committed to the repo. See .env.example for the full list.

Fail-closed by design: in `prod`, `validate()` is called at boot and refuses
to start on a missing/placeholder secret instead of silently running insecure.
"""

import os

DEFAULT_WEBHOOK_SECRET = "dev-secret-change-me"


def _get_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self) -> None:
        self.environment = os.getenv("FINANCE_ENV", "dev").strip().lower()
        self.database_url = os.getenv(
            "DATABASE_URL", "sqlite:////workspace/service/finance.db"
        )
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.webhook_secret = os.getenv("WEBHOOK_SECRET", DEFAULT_WEBHOOK_SECRET)
        self.admin_user_id = os.getenv("ADMIN_USER_ID", "")

        self.shared_prompt_threshold = int(os.getenv("SHARED_PROMPT_THRESHOLD", "500"))
        self.ask_everything = _get_bool("ASK_EVERYTHING", False)
        self.quiet_hours_start = int(os.getenv("QUIET_HOURS_START", "23"))
        self.quiet_hours_end = int(os.getenv("QUIET_HOURS_END", "7"))

        self.llm_api_key = os.getenv("USER_LLM_API_KEY", "")
        self.llm_base_url = os.getenv("USER_LLM_BASE_URL", "")
        self.llm_model = os.getenv("USER_LLM_MODEL", "")

        self.rate_limit_per_min = int(os.getenv("RATE_LIMIT_PER_MIN", "0"))
        self.log_dir = os.getenv("LOG_DIR", "")

    def validate(self, require_bot: bool = False) -> None:
        """Fail fast at boot if production is missing a required secret.

        `require_bot` is True for the bot process (needs a token); the API
        process doesn't. Never trusts a placeholder/empty value in prod.
        """
        if self.environment != "prod":
            return
        errors = []
        if not self.webhook_secret or self.webhook_secret == DEFAULT_WEBHOOK_SECRET:
            errors.append("WEBHOOK_SECRET must be set to a non-placeholder value")
        if not self.admin_user_id:
            errors.append("ADMIN_USER_ID must be set (bot allowlist)")
        if require_bot and not self.telegram_bot_token:
            errors.append("TELEGRAM_BOT_TOKEN must be set")
        if errors:
            raise RuntimeError("Production config invalid: " + "; ".join(errors))


settings = Settings()
