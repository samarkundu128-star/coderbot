import sys
import traceback
import os
import google.generativeai as genai
from github import Github
from src.config.settings import settings

# Gemini AI configure kar rahe hain safely
try:
    genai.configure(api_key=settings.GEMINI_API_KEY.get_secret_value())
except Exception:
    pass

def commit_code_to_github(file_path: str, new_content: str):
    """Bot khud decision lekar GitHub par code push karega"""
    try:
        # Render ke environment variables se token aur repo name uthayenge
        token = os.getenv("GITHUB_TOKEN")
        repo_name = os.getenv("REPO_NAME")
        
        if not token or not repo_name:
            print("Auto-Healer Error: GITHUB_TOKEN ya REPO_NAME missing hai!")
            return

        g = Github(token)
        repo = g.get_repo(repo_name)
        
        # GitHub se file ki current state nikalna
        contents = repo.get_contents(file_path, ref="main")
        
        # Commit message ke saath file update karna
        repo.update_file(
            path=file_path,
            message="🤖 AI Auto-Heal: Fixed runtime crash automatically",
            content=new_content,
            sha=contents.sha,
            branch="main"
        )
        print(f"GitHub par {file_path} safely modify ho gayi hai!")
    except Exception as e:
        print(f"GitHub API push failed: {e}")

def ai_autonomous_healer(exc_type, exc_value, exc_traceback):
    """Jab bhi bot crash hone lagega, yeh dimaag lagayega"""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    error_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    
    tb = exc_traceback
    while tb.tb_next:
        tb = tb.tb_next
    filename = tb.tb_frame.f_code.co_filename

    # Sirf hamare project ki files ko hi fix karein, system libraries ko nahi
    if "site-packages" in filename or not os.path.exists(filename):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    with open(filename, "r") as f:
        original_code = f.read()

    relative_path = os.path.relpath(filename, os.getcwd())

    prompt = f"""
    Mera Python bot crash ho gaya hai. Kripya is file ko theek karne me decision lein.
    Mujhe sirf aur sirf sahi kiya hua POORA CODE chahiye, aur kuch bhi mat likhna (no explanations, no extra text).

    [ERROR LOGS]:
    {error_msg}

    [FILE PATH]: {relative_path}
    
    [ORIGINAL CODE]:
    {original_code}
    """

    try:
        model = genai.GenerativeModel('gemini-pro')
        response = model.generate_content(prompt)
        fixed_code = response.text
        
        # Markdown backticks hatana agar AI ne laga diye hon toh
        if fixed_code.startswith("```"):
            fixed_code = "\n".join(fixed_code.split("\n")[1:-1])
            
        commit_code_to_github(relative_path, fixed_code.strip())
    except Exception as ai_err:
        print(f"Autonomous healer failed: {ai_err}")

    sys.__excepthook__(exc_type, exc_value, exc_traceback)

def setup_auto_healer():
    """Isko main.py me initialize karenge"""
    sys.excepthook = ai_autonomous_healer
          
