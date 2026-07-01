import sys
import traceback
import os
from google import genai
from google.genai import types
from github import Github
from src.config.settings import settings

# Naya Google Gen AI SDK client initialize kar rahe hain safely
# Yeh automatic environment variable 'GEMINI_API_KEY' se token utha leta hai
try:
    client = genai.Client()
except Exception as e:
    print(f"Auto-Healer Initialization Error: {e}")
    client = None

def commit_code_to_github(file_path: str, new_content: str):
    """Bot khud decision lekar GitHub par code push karega"""
    try:
        token = os.getenv("GITHUB_TOKEN")
        repo_name = os.getenv("REPO_NAME")
        
        if not token or not repo_name:
            print("Auto-Healer Error: GITHUB_TOKEN ya REPO_NAME missing hai!")
            return

        g = Github(token)
        repo = g.get_repo(repo_name)
        
        contents = repo.get_contents(file_path, ref="main")
        
        repo.update_file(
            path=file_path,
            message="🤖 AI Auto-Heal: Upgraded to latest Google Gen AI SDK and fixed code runtime",
            content=new_content,
            sha=contents.sha,
            branch="main"
        )
        print(f"GitHub par {file_path} safely modify ho gayi hai!")
    except Exception as e:
        print(f"GitHub API push failed: {e}")

def ai_autonomous_healer(exc_type, exc_value, exc_traceback):
    """Jab bhi bot crash hone lagega, yeh dimaag lagayega"""
    if issubclass(exc_type, KeyboardInterrupt) or client is None:
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    error_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    
    tb = exc_traceback
    while tb.tb_next:
        tb = tb.tb_next
    filename = tb.tb_frame.f_code.co_filename

    if "site-packages" in filename or not os.path.exists(filename):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    with open(filename, "r") as f:
        original_code = f.read()

    relative_path = os.path.relpath(filename, os.getcwd())

    prompt = f"""
    Mera Python bot crash ho gaya hai. Kripya is file ko theek karne me decision lein.
    Mujhe sirf aur sirf sahi kiya hua POORA CODE chahiye, aur kuch bhi mat likhna (no explanations, no extra text, no markdown backticks).

    [ERROR LOGS]:
    {error_msg}

    [FILE PATH]: {relative_path}
    
    [ORIGINAL CODE]:
    {original_code}
    """

    try:
        # Naye SDK me 'gemini-2.5-flash' ya 'gemini-2.5-pro' models use hote hain
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        fixed_code = response.text
        
        if fixed_code.startswith("```"):
            fixed_code = "\n".join(fixed_code.split("\n")[1:-1])
            
        commit_code_to_github(relative_path, fixed_code.strip())
    except Exception as ai_err:
        print(f"Autonomous healer failed: {ai_err}")

    sys.__excepthook__(exc_type, exc_value, exc_traceback)

def setup_auto_healer():
    """Isko main.py me initialize karenge"""
    sys.excepthook = ai_autonomous_healer
