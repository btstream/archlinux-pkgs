#!/usr/bin/env python3
"""以最后成功工作流为增量基线，避免失败或被替换的 pending run 漏掉变更。"""

import json
import os
from pathlib import Path
import subprocess
import urllib.parse
import urllib.request


def main():
    full = os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch"
    before = ""
    if not full:
        query = urllib.parse.urlencode({
            "branch": os.environ["GITHUB_REF_NAME"], "status": "success", "per_page": 1,
        })
        url = (
            f'{os.environ.get("GITHUB_API_URL", "https://api.github.com")}'
            f'/repos/{os.environ["GITHUB_REPOSITORY"]}'
            f"/actions/workflows/repository.yml/runs?{query}"
        )
        request = urllib.request.Request(url, headers={
            "Authorization": f'Bearer {os.environ["GH_TOKEN"]}',
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with urllib.request.urlopen(request, timeout=30) as response:
            runs = json.load(response)["workflow_runs"]
        if runs:
            before = runs[0]["head_sha"]
            full = subprocess.run(
                ["git", "merge-base", "--is-ancestor", before, "HEAD"],
                check=False,
            ).returncode != 0
        else:
            full = True
    with Path(os.environ["GITHUB_ENV"]).open("a") as environment:
        environment.write(f"BEFORE_SHA={before}\nFULL_BUILD={str(full).lower()}\n")
    print(f'构建基线：{"全量" if full else before}')


if __name__ == "__main__":
    main()
