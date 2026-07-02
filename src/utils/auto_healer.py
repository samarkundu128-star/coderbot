import sys
import time
import hashlib
import threading
import traceback
import ast
import os
from groq import Groq
from github import Github

# --- Groq Client initialize kar rahe hain safely ---
try:
    groq_api_key = os.getenv("GROQ_API_KEY")
    client = Groq(api_key=groq_api_key) if groq_api_key else None
except Exception as e:
    print(f"Auto-Healer Initialization Error: {e}")
    client = None

# NOTE: llama3-8b-8192, llama3-70b-8192, llama-3.3-70b-versatile, aur
# llama-3.1-8b-instant sab deprecate ho chuke hain (last Groq update: 17 June 2026).
# Primary = accurate bug-fixing ke liye, Fallback = agar primary fail ho jaye.
GROQ_MODEL_PRIMARY = "openai/gpt-oss-120b"
GROQ_MODEL_FALLBACK = "openai/gpt-oss-20b"

# --- Crash-loop protection: same error baar baar fix karne ki koshish na kare ---
_recent_fix_attempts = {}  # {error_hash: (timestamp, attempt_count)}
_MAX_ATTEMPTS_PER_ERROR = 3
_COOLDOWN_SECONDS = 3600  # 1 ghanta


def _get_error_hash(filename: str, error_msg: str) -> str:
    """Same file + same error type ko identify karne ke liye hash banata hai."""
    key = f"{filename}:{error_msg.splitlines()[-1] if error_msg else ''}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _is_rate_limited(error_hash: str) -> bool:
    """Agar yehi error bahut baar aa chuka hai, toh Healer ko rok dein."""
    now = time.time()
    if error_hash in _recent_fix_attempts:
        last_time, count = _recent_fix_attempts[error_hash]
        if now - last_time < _COOLDOWN_SECONDS:
            if count >= _MAX_ATTEMPTS_PER_ERROR:
                print(f"⛔ Auto-Healer: Is error ko {count} baar fix karne ki koshish ho chuki hai. "
                      f"Cooldown active hai — manual review zaroori hai.")
                return True
            _recent_fix_attempts[error_hash] = (last_time, count + 1)
            return False
    _recent_fix_attempts[error_hash] = (now, 1)
    return False


def _is_valid_python(code: str) -> bool:
    """Recheck step: AI ka generated code syntactically valid hai ya nahi, commit se pehle verify karta hai."""
    try:
        ast.parse(code)
        return True
    except SyntaxError as e:
        print(f"⚠️ Auto-Healer: AI-generated code invalid hai (SyntaxError: {e}). Commit skip kar raha hoon.")
        return False


def commit_to_github_and_trigger(file_path: str, new_content: str):
    """GitHub par push karega jisse Render par automatic deploy trigger ho jayega."""
    try:
        token = os.getenv("GITHUB_TOKEN")
        repo_name = os.getenv("REPO_NAME")

        if not token or not repo_name:
            print("Auto-Healer Error: GITHUB_TOKEN ya REPO_NAME missing hai!")
            return False

        g = Github(token)
        repo = g.get_repo(repo_name)
        contents = repo.get_contents(file_path, ref="main")

        repo.update_file(
            path=file_path,
            message="🤖 AI Auto-Heal: Fixed bug & triggered auto-redeploy",
            content=new_content,
            sha=contents.sha,
            branch="main"
        )
        print("🚀 Code GitHub par push ho gaya! Deployment automatically shuru ho raha hai...")
        return True
    except Exception as e:
        print(f"GitHub API push failed: {e}")
        return False


def _generate_fix(prompt: str, model: str) -> str | None:
    """Ek model se fix generate karne ki koshish karta hai. Fail hone par None return karta hai."""
    try:
        chat_completion = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.2,
            timeout=30,
        )
        fixed_code = chat_completion.choices[0].message.content

        if fixed_code.startswith("```"):
            fixed_code = "\n".join(fixed_code.split("\n")[1:-1])

        return fixed_code.strip()
    except Exception as ai_err:
        print(f"⚠️ Auto-Healer: Model '{model}' se fix generate nahi hua: {ai_err}")
        return None


def _heal_in_background(filename: str, relative_path: str, error_msg: str, original_code: str):
    """
    Yeh function ek background thread mein chalta hai taaki Groq/GitHub ke
    blocking network calls FastAPI ke event loop ko block na karein.
    """
    error_hash = _get_error_hash(filename, error_msg)
    if _is_rate_limited(error_hash):
        return

    prompt = f"""
    Mera Python bot crash ho gaya hai. Kripya is file ko theek karo.
    Mujhe sirf aur sirf sahi kiya hua POORA CODE chahiye, aur kuch bhi mat likhna
    (no explanations, no extra text, no markdown backticks).

    [ERROR LOGS]:
    {error_msg}

    [FILE PATH]: {relative_path}

    [ORIGINAL CODE]:
    {original_code}
    """

    # Step 1: Primary model try karein
    fixed_code = _generate_fix(prompt, GROQ_MODEL_PRIMARY)

    # Step 2: Agar primary fail ho ya invalid code de, fallback model try karein
    if not fixed_code or not _is_valid_python(fixed_code):
        print("🔄 Auto-Healer: Fallback model try kar raha hoon...")
        fixed_code = _generate_fix(prompt, GROQ_MODEL_FALLBACK)

    # Step 3: Final recheck — agar ab bhi invalid hai, commit mat karo
    if not fixed_code or not _is_valid_python(fixed_code):
        print("❌ Auto-Healer: Dono models se valid fix nahi mila. Commit skip kar raha hoon — manual review zaroori.")
        return

    commit_to_github_and_trigger(relative_path, fixed_code)


def _process_exception(exc_type, exc_value, exc_traceback):
    """Common logic jo sync aur async dono exception paths se use hota hai."""
    if client is None or (exc_type and issubclass(exc_type, KeyboardInterrupt)):
        return

    error_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))

    tb = exc_traceback
    while tb and tb.tb_next:
        tb = tb.tb_next
    if tb is None:
        return

    filename = tb.tb_frame.f_code.co_filename

    if "site-packages" in filename or not os.path.exists(filename):
        return

    try:
        with open(filename, "r") as f:
            original_code = f.read()
    except Exception:
        return

    relative_path = os.path.relpath(filename, os.getcwd())

    # Network calls background thread mein taaki event loop block na ho
    threading.Thread(
        target=_heal_in_background,
        args=(filename, relative_path, error_msg, original_code),
        daemon=True,
    ).start()


def ai_autonomous_healer(exc_type, exc_value, exc_traceback):
    """Synchronous top-level crashes ke liye (sys.excepthook)."""
    _process_exception(exc_type, exc_value, exc_traceback)
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


def ai_asyncio_exception_handler(loop, context):
    """
    Async/coroutine crashes ke liye (FastAPI/uvloop ke andar jo errors aate hain,
    wo yahan se pakde jaate hain — sys.excepthook se NAHI).
    """
    exception = context.get("exception")
    if exception is not None:
        _process_exception(type(exception), exception, exception.__traceback__)
    else:
        print(f"⚠️ Auto-Healer: Async error bina exception object ke: {context.get('message')}")

    # Default asyncio error logging bhi chalne dein
    loop.default_exception_handler(context)


def setup_auto_healer():
    """
    Sirf synchronous/top-level crash hook set karta hai. Yeh module import
    time par call hoti hai (uvicorn start hone se pehle), isliye abhi
    actual event loop (uvloop) exist nahi karta — async handler ALAG se
    register_async_exception_handler() se, FastAPI lifespan ke andar
    (jab actual loop chal raha ho) call karna zaroori hai.
    """
    sys.excepthook = ai_autonomous_healer
    print("✅ Auto-Healer: Sync exception hook set ho gaya.")


def register_async_exception_handler():
    """
    FastAPI lifespan ke andar (async context, jab uvloop actually chal raha
    ho) call karein. asyncio.get_running_loop() use karta hai taaki sahi,
    actual loop par handler set ho — asyncio.get_event_loop() import-time
    par galat/discarded loop de sakta tha.
    """
    try:
        import asyncio
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(ai_asyncio_exception_handler)
        print("✅ Auto-Healer: Async exception handler register ho gaya (FastAPI crashes ab catch honge)")
    except RuntimeError:
        print("⚠️ Auto-Healer: Koi running event loop nahi mila — async handler register nahi ho paya.")
    except Exception as e:
        print(f"⚠️ Auto-Healer: Async exception handler set nahi ho paya: {e}")
