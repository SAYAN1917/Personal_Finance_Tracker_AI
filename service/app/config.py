"""Application configuration.

All secrets come from environment variables - never from code or config files
committed to the repo. See .env.example for the full list.
"""

import os


def _get_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self) -> None:
        self.database_url = os.getenv(
            "DATABASE_URL", "sqlite:////workspace/service/finance.db"
        )
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.webhook_secret = os.getenv("WEBHOOK_SECRET", "dev-secret-change-me")
        self.admin_user_id = os.getenv("ADMIN_USER_ID", "")

        self.shared_prompt_threshold = int(os.getenv("SHARED_PROMPT_THRESHOLD", "500"))
        self.ask_everything = _get_bool("ASK_EVERYTHING", False)
        self.quiet_hours_start = int(os.getenv("QUIET_HOURS_START", "23"))
        self.quiet_hours_end = int(os.getenv("QUIET_HOURS_END", "7"))

        self.llm_api_key = os.getenv("USER_LLM_API_KEY", "")
        self.llm_base_url = os.getenv("USER_LLM_BASE_URL", "")
        self.llm_model = os.getenv("USER_LLM_MODEL", "")


settings = Settings()
