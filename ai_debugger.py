import os
import asyncio
import google.generativeai as genai
from src.config.settings import settings

# Gemini API Client Configuration
try:
    genai.configure(api_key=settings.GEMINI_API_KEY.get_secret_value())
    model = genai.GenerativeModel("gemini-2.5-flash-preview-09-2025")
except Exception as e:
    print(f"❌ Gemini API Configure karne me dikkat aayi: {e}")
    exit(1)

async def analyze_file(file_path: str) -> str:
    """Ek single file ko read karke uski dikkat aur solution AI se nikalti hai."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            code_content = f.read()
        
        prompt = (
            f"Aap ek expert Python Debugger hain. Niche diye gaye code ko achhe se analyze karein.\n"
            f"Agar isme koi Syntax Error, Logical Bug, Missing Import, ya Pydantic/SQLAlchemy ka galat usage hai, "
            f"toh use point out karein aur uska clean, corrected code solution dein.\n\n"
            f"File Path: {file_path}\n"
            f"Code:\n```python\n{code_content}\n```"
        )
        
        # Asynchronous API Call
        response = await model.generate_content_async(prompt)
        return f"## 📄 File: {file_path}\n\n{response.text}\n\n---\n"
        
    except Exception as e:
        return f"## 📄 File: {file_path}\n❌ Is file ko analyze nahi kiya ja saka. Error: {str(e)}\n\n---\n"

async def main():
    print("🔍 Project directory scanning shuru ho gayi hai...")
    report_content = "# 🛠️ Automated AI Debugging & Solution Report\n\n"
    tasks = []
    
    # Poore project folders ko scan karna
    for root, dirs, files in os.walk("."):
        # फालतू system folders ko skip karne ke liye
        if any(ignored in root for ignored in ["venv", ".git", "__pycache__", ".pytest_cache"]):
            continue
            
        for file in files:
            # Sirf Python files ko target karenge
            if file.endswith(".py") and file != "ai_debugger.py":
                file_path = os.path.join(root, file)
                print(f"Added to queue: {file_path}")
                tasks.append(analyze_file(file_path))
    
    if not tasks:
        print("❌ Koi Python file nahi mili!")
        return

    print(f"🤖 Total {len(tasks)} files ko Gemini AI se analyze karwaya ja raha hai...")
    results = await asyncio.gather(*tasks)
    
    for res in results:
        report_content += res
        
    # Output report file generate karna
    with open("debug_report.md", "w", encoding="utf-8") as report_file:
        report_file.write(report_content)
        
    print("✅ Report taiyar hai! Apne folder me 'debug_report.md' file check karein.")

if __name__ == "__main__":
    asyncio.run(main())
