APP_TITLE = "AI Interview Demo"
APP_VERSION = "1.0.0"
SESSION_ID = "demo-session"
REPORT_CODE = "apple"
CORS_DEFAULT_ORIGINS = [
    # HTTP dev
    "http://127.0.0.1:2020",
    "http://localhost:2020",
    # HTTPS default (start_app.bat)
    "https://127.0.0.1:2020",
    "https://localhost:2020",
]

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
TEXT_EXTENSIONS = {".txt", ".md", ".rtf"}
WORD_EXTENSIONS = {".docx", ".doc"}

# Chat/completions models for official OpenAI (when not using Ollama base URL).
OPENAI_CHAT_MODELS = [
    "gpt-4o-mini",
]
