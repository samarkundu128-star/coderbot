import structlog
from github import Github, GithubException
from src.config.settings import settings

logger = structlog.get_logger(__name__)

_github_client = Github(settings.GITHUB_TOKEN.get_secret_value())


def push_files(files: list[dict], commit_message: str) -> str | None:
    """
    files: [{"file_path": "...", "content": "..."}, ...]
    Har file ko create ya update karta hai (jo bhi applicable ho) aur
    aakhri commit ka SHA return karta hai (deploy-watcher ke liye).
    Yeh function synchronous hai (PyGithub sync SDK hai) — caller ise
    asyncio.to_thread() ke andar call kare taaki event loop block na ho.
    """
    repo = _github_client.get_repo(settings.REPO_NAME)
    last_sha = None

    for f in files:
        path = (f.get("file_path") or "").strip().lstrip("/")
        content = f.get("content", "")
        if not path:
            continue

        try:
            existing = repo.get_contents(path)
            result = repo.update_file(
                path=path,
                message=commit_message,
                content=content,
                sha=existing.sha,
            )
            logger.info("File updated on GitHub", path=path)
        except GithubException:
            result = repo.create_file(
                path=path,
                message=commit_message,
                content=content,
            )
            logger.info("File created on GitHub", path=path)

        commit = result.get("commit") if isinstance(result, dict) else None
        if commit is not None:
            last_sha = commit.sha

    return last_sha
