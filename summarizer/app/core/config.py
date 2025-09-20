from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    ENV: str = "dev"
    PORT: int = 9000

    PROVIDER: str = "openai_compat"          # or "anthropic"
    OPENAI_API_KEY: str | None = None
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    MODEL: str = "gpt-4o-mini"

    QUEUE_URL: str = "redis://localhost:6379/0"

    RESULTS_BUCKET: str = "local://data/summaries"
    TRANSCRIPTS_BASE: str = "local://data/transcripts"

    WEBHOOK_SECRET: str | None = None

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()  # singleton
