import sys
import traceback
import os
from groq import Groq
from github import Github

# Groq Client ko safely clean tarike se initialize kar rahe hain
# Isse 'proxies' wala unexpected keyword argument error nahi aayega
try:
    groq_api_key = os.getenv("GROQ_API_KEY")
    if groq_api_key:
        client = Groq(api_key=groq_api_key)
    else:
        print("Auto-Healer Error: GROQ_API_KEY environment variable nahi mila!")
        client = None
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
            message="🤖 AI Auto-Heal: Upgraded to Groq API & Fixed runtime crash",
            content=new_content,
            sha=contents.sha,
            branch="main"
        )
        print(f"GitHub par {file_path} safely modify ho gayi hai!")
    except Exception as e:
        print(f"GitHub API push failed: {e}")

def ai_autonomous_healer(exc_type, exc_value, exc_traceback):
    """Groq API ka use karke bot khud ko theek karega"""
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
        # Groq par llama3-8b-8192 model use kar rahe hain jo fast aur accurate hai
        chat_completion = client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            model="llama3-8b-8192",
        )
        fixed_code = chat_completion.choices[0].message.content

        if fixed_code.startswith("```"):
            fixed_code = "\n".join(fixed_code.split("\n")[1:-1])

        commit_code_to_github(relative_path, fixed_code.strip())
    except Exception as ai_err:
        print(f"Autonomous healer failed: {ai_err}")

    sys.__excepthook__(exc_type, exc_value, exc_traceback)

def setup_auto_healer():
    """Isko main.py me initialize karenge"""
    sys.excepthook = ai_autonomous_healer
