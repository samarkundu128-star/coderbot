CODE DOCTOR — Startup Diagnostic Scanner
==========================================
Ye module koi bhi cheez run ya import NAHI karta (isliye khud crash nahi
ho sakta). Sirf `ast` ke through poore `src/` ke saare .py files ko
static parse karta hai aur ye 3 tarah ki problems dhoondta hai:

  1. IMPORT MISMATCH  — `from src.x import y` diya hai, lekin `y` naam
     `src/x.py` ke andar exist hi nahi karta (jaisa pehle ke crashes mein
     hua tha: addlink_command_handler, subscription_recheck_callback_handler, etc.)

  2. ARGUMENT COUNT MISMATCH — function ko jitne arguments diye ja rahe
     hain call site par, wo uski actual definition se match nahi karte.

  3. ASYNC/SYNC MISUSE — kisi sync (non-async) function ko `await` kiya
     ja raha hai, ya vice-versa.

Isse pura report Render ke logs mein DEPLOY START hote hi dikh jaata hai —
chahe baad mein Python import-time par crash ho ya na ho. Isse agar koi
naya mismatch aaye, to exact file + line pehle hi pata chal jayega,
guess karne ki zaroorat nahi padegi.

Ye function sirf LOG karta hai, kabhi bhi process ko crash ya exit
nahi karta — production startup ko block nahi karega.
"""

import ast
import os

try:
    import structlog
    logger = structlog.get_logger(__name__)
except Exception:
    class _FallbackLogger:
        """Agar structlog kisi wajah se available na ho, tab bhi diagnostics print ho jayein."""
        def info(self, msg, **kwargs):
            print(f"[INFO] {msg} {kwargs}")

        def warning(self, msg, **kwargs):
            print(f"[WARNING] {msg} {kwargs}")

        def error(self, msg, **kwargs):
            print(f"[ERROR] {msg} {kwargs}")

    logger = _FallbackLogger()

# Jahan se scan shuru karna hai (project root ke andar 'src' folder)
_SRC_ROOT = "src"


def _find_py_files(root: str) -> list[str]:
    files = []
    for dirpath, dirs, filenames in os.walk(root):
        if ".git" in dirpath or "__pycache__" in dirpath:
            continue
        for f in filenames:
            if f.endswith(".py"):
                files.append(os.path.join(dirpath, f))
    return files


def _modpath_to_file(mod: str) -> str | None:
    p = mod.replace(".", "/") + ".py"
    return p if os.path.exists(p) else None


def _get_module_defs(filepath: str):
    """
    Ek file ke top-level mein defined saare names return karta hai, plus
    har function ke liye (min_args, max_args, has_vararg, is_async).
    """
    with open(filepath, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=filepath)

    func_sigs = {}
    all_names = set()

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            total = len(args.args)
            defaults = len(args.defaults)
            min_args = total - defaults
            has_vararg = args.vararg is not None
            func_sigs[node.name] = (min_args, total, has_vararg, isinstance(node, ast.AsyncFunctionDef))
            all_names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            all_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    all_names.add(t.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                all_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                all_names.add((alias.asname or alias.name).split(".")[0])

    return func_sigs, all_names


def run_full_diagnostics() -> dict:
    """
    Poore codebase ko scan karke ek detailed report banata hai aur usse
    structlog ke through print karta hai. Kabhi exception raise nahi
    karta — koi bhi internal error sirf ek warning ki tarah log hoga.

    Returns: summary dict jisme total files, total issues count, aur
    poori issues list hoti hai (agar caller ko programmatically chahiye ho).
    """
    report = {
        "files_scanned": 0,
        "syntax_errors": [],
        "import_mismatches": [],
        "signature_mismatches": [],
        "async_sync_misuse": [],
    }

    try:
        py_files = _find_py_files(_SRC_ROOT)
        report["files_scanned"] = len(py_files)
        module_cache = {}

        for pf in py_files:
            try:
                with open(pf, "r", encoding="utf-8") as f:
                    source = f.read()
                tree = ast.parse(source, filename=pf)
            except SyntaxError as e:
                report["syntax_errors"].append(f"{pf}: line {e.lineno} — {e.msg}")
                continue
            except Exception as e:
                report["syntax_errors"].append(f"{pf}: could not parse — {e}")
                continue

            imported_from = {}

            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src"):
                    modfile = _modpath_to_file(node.module)
                    if modfile is None:
                        report["import_mismatches"].append(
                            f"{pf}: imports from module '{node.module}' — file not found on disk"
                        )
                        continue
                    if modfile not in module_cache:
                        try:
                            module_cache[modfile] = _get_module_defs(modfile)
                        except Exception as e:
                            report["syntax_errors"].append(f"{modfile}: could not parse — {e}")
                            module_cache[modfile] = ({}, set())
                    func_sigs, all_names = module_cache[modfile]

                    for alias in node.names:
                        name = alias.name
                        if name == "*":
                            continue
                        local_name = alias.asname or alias.name
                        if name not in all_names:
                            report["import_mismatches"].append(
                                f"{pf} (line {node.lineno}): imports '{name}' from '{node.module}' "
                                f"({modfile}) — NAME NOT FOUND. Available names: "
                                f"{', '.join(sorted(all_names)) or '(none)'}"
                            )
                        else:
                            imported_from[local_name] = (node.module, name, func_sigs.get(name))

            # Argument-count checks on direct calls
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    fname = node.func.id
                    if fname in imported_from:
                        mod, orig_name, siginfo = imported_from[fname]
                        if siginfo is None:
                            continue
                        min_args, max_args, has_vararg, _is_async = siginfo
                        total_given = len(node.args) + len(node.keywords)
                        if not has_vararg and (total_given < min_args or total_given > max_args):
                            report["signature_mismatches"].append(
                                f"{pf} (line {node.lineno}): calls {fname}(...) with {total_given} "
                                f"argument(s), but {mod}.{orig_name} expects between "
                                f"{min_args} and {max_args}"
                            )

            # Async/sync misuse checks
            for node in ast.walk(tree):
                if isinstance(node, ast.Await) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                    fname = node.value.func.id
                    if fname in imported_from:
                        mod, orig_name, siginfo = imported_from[fname]
                        if siginfo and not siginfo[3]:
                            report["async_sync_misuse"].append(
                                f"{pf} (line {node.lineno}): uses 'await {fname}(...)' but "
                                f"{mod}.{orig_name} is a SYNC function, not async"
                            )

    except Exception as e:
        logger.error("code_doctor_internal_error", error=str(e))

    total_issues = (
        len(report["syntax_errors"])
        + len(report["import_mismatches"])
        + len(report["signature_mismatches"])
        + len(report["async_sync_misuse"])
    )

    # --- Ab poori report ko clearly logs mein print karte hain ---
    logger.info(
        "🩺 CODE DOCTOR — startup scan complete",
        files_scanned=report["files_scanned"],
        total_issues=total_issues,
    )

    if total_issues == 0:
        logger.info("✅ CODE DOCTOR: Koi bhi import/signature/async mismatch nahi mila. Codebase clean hai.")
        return report

    for item in report["syntax_errors"]:
        logger.error("🩺 CODE DOCTOR [SYNTAX ERROR]", detail=item)
    for item in report["import_mismatches"]:
        logger.error("🩺 CODE DOCTOR [IMPORT MISMATCH]", detail=item)
    for item in report["signature_mismatches"]:
        logger.error("🩺 CODE DOCTOR [ARGUMENT MISMATCH]", detail=item)
    for item in report["async_sync_misuse"]:
        logger.error("🩺 CODE DOCTOR [ASYNC/SYNC MISUSE]", detail=item)

    logger.warning(
        f"🩺 CODE DOCTOR: Total {total_issues} problem(s) mile — upar detail logs mein dekho."
    )

    return report


if __name__ == "__main__":
    # Standalone bhi chala sakte ho: python -m src.utils.code_doctor
    run_full_diagnostics()
