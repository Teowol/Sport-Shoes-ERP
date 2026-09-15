"""Settings for local development on Windows."""

from .settings import *  # noqa: F403
from .settings import BASE_DIR

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
AI_EMBEDDING_MODEL_PATH = str(BASE_DIR / ".local" / "ai-models" / "multilingual-e5-small")
