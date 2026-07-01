import sys
import traceback
import os
import google.generativeai as genai
from github import Github  # Iske liye 'PyGithub' requirements.txt me dalna hoga
from src.config.settings import settings

# Gemini aur GitHub configure kar rahe hain
genai.configure(api_key=settings.GEMINI_API_KEY.get_secret_value())

def commit_code_to_github(file_path: str, new_content: str):
    """Bot khud GitHub par code push karega"""
    try:
        # Aapko settings me GITHUB_TOKEN aur REPO_NAME jorna padega
        g = Github(os.getenv("GITHUB_TOKEN")) 
        repo = g.get_repo(os.getenv("REPO_NAME")) # Jaise: "samarkundu128-star/coderbot"
        
        # GitHub se file ki details nikalna (SHA hash chahiye hota hai update ke liye)
        contents = repo.get_contents(file_path, ref="main")
        
        # Commit message ke saath file update karna
        repo.update_file(
            path=file_path,
            message="🤖 AI Auto-Heal: Fixed runtime crash automatically",
            content=new_content,
            sha=contents.sha,
            branch="main"
        )
        print("GitHub par code safely modify aur commit ho gaya hai!")
    except Exception as e:
        print(f"GitHub par push karne me dikkat aayi: {e}")

def ai_autonomous_healer(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    error_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    
    tb = exc_traceback
    while tb.tb_next:
        tb = tb.tb_next
    filename = tb.tb_frame.f_code.co_filename

    if os.path.exists(filename):
        with open(filename, "r") as f:
            original_code = f.read()

    # Relative path nikalna GitHub ke liye (jaise: src/main.py)
    relative_path = os.path.relpath(filename, os.getcwd())

    prompt = f"""
    Mera Python bot crash ho gaya hai. Kripya is file ko theek karein.
    Mujhe sirf aur sirf sahi kiya hua POORA CODE chahiye markdown block me, baki koi baat mat likhna.

    [ERROR]: {error_msg}
    [FILE PATH]: {relative_path}
    [CODE]:
    {original_code}
    """

    try:
        model = genai.GenerativeModel('gemini-pro')
        response = model.generate_content(prompt)
        fixed_code = response.text.replace("```python", "").replace("```", "").strip()
        
        # Khud decision lekar GitHub par push kar dena
        commit_code_to_github(relative_path, fixed_code)
        
    except Exception as ai_err:
        print(f"Autonomous fixing failed: {ai_err}")

    sys.__excepthook__(exc_type, exc_value, exc_traceback)
