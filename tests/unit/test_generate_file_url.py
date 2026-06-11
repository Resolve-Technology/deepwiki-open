"""Tests for blob-URL construction (the .git-stripping fix)."""
from api.wiki_prompts import generate_file_url


def test_gitlab_strips_dot_git():
    url = generate_file_url(
        "https://gitlab.reslv.one/poc/code2_sqlcbl_cal101.git",
        "gitlab", "CAL101.txt", "main")
    assert url == "https://gitlab.reslv.one/poc/code2_sqlcbl_cal101/-/blob/main/CAL101.txt"


def test_github_without_dot_git_unchanged():
    url = generate_file_url("https://github.com/o/r", "github", "a.ts", "main")
    assert url == "https://github.com/o/r/blob/main/a.ts"


def test_local_returns_bare_path():
    assert generate_file_url("", "local", "CAL101.txt", "main") == "CAL101.txt"
