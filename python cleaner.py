import os

def clean_top_lines():
    # Saari python files ko scan karega
    for root, dirs, files in os.walk("."):
        # Hidden folders (.git, .venv) ko skip karne ke liye
        if "venv" in root or ".git" in root:
            continue
            
        for file in files:
            if file.endswith(".py"):
                file_path = os.path.join(root, file)
                with open(file_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                
                if lines:
                    first_line = lines[0].strip().lower()
                    # Agar pehli line me sirf 'python' ya khali 'import sys' capital me ho
                    if first_line == "python" or first_line == "import":
                        print(f"Cleaning: {file_path}")
                        with open(file_path, "w", encoding="utf-8") as f:
                            f.writelines(lines[1:]) # Pehli line delete kar dega

if __name__ == "__main__":
    clean_top_lines()
