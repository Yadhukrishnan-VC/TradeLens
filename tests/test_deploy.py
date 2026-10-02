import pytest
import re


def test_env_ignored_by_git():
    """.env must be git-ignored and untracked."""
    import subprocess
    # check git ignores .env
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0, ".env is still tracked by git"

    # check .gitignore contains .env
    with open(".gitignore") as f:
        gi = f.read()
    assert ".env" in gi, ".env not found in .gitignore"


def test_docker_compose_required_api_token():
    """Both app and worker services must have a required API_TOKEN in compose."""
    with open("docker-compose.yml") as f:
        raw = f.read()
    for svc in ("app", "worker"):
        # find the service section lines until 'ports:' or end
        lines = raw.split("\n")
        in_section = False
        api_token_line = None
        for line in lines:
            if f"  {svc}:" in line:
                in_section = True
            if in_section and line.strip().startswith("ports:"):
                break
            if in_section and line.strip().startswith("API_TOKEN:"):
                api_token_line = line
                break
        assert api_token_line is not None, (
            f"API_TOKEN not configured in {svc} service of docker-compose.yml"
        )
        # the value must contain the ?fail guard (not a bare value)
        assert "${API_TOKEN?" in api_token_line, (
            f"API_TOKEN in {svc} is not a required guard; value={api_token_line}"
        )


def test_readme_no_no_login():
    """README must not contain 'no login yet' (case-insensitive)."""
    with open("README.md") as f:
        readme = f.read()
    assert "no login yet" not in readme.lower(), (
        "README still contains 'no login yet' — update the documented auth status"
    )