#: The product name every user-facing surface prints (26 Sep 2026: the app is
#: "Karnex Orbit"; "Karnex" alone stays the COMPANY name on invoices / letters).
#: `APP_TITLE` is the same value under the older name main.py still imports.
APP_NAME = "Karnex Orbit"
APP_TITLE = APP_NAME
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

