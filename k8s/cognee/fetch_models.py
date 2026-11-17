"""Download the model files of models.txt into a Hugging Face cache, at build time only.

    python -I fetch_models.py models.txt /opt/cognee/hf/hub

Each file goes to <cache>/models--<org>--<name>/snapshots/<commit>/<path>, the layout that
huggingface_hub reads offline (the files are real files, not links to blobs/). A file is kept
only when its size and SHA-256 are the ones in models.txt; anything else stops the build.
Then <cache>/models--<org>--<name>/refs/main holds the commit, so a lookup of the repo with no
revision (what cognee, gliner2 and fastembed do) finds exactly that commit, with
HF_HUB_OFFLINE=1. Standard library only; it runs nothing of the models.
"""

import hashlib
import os
import sys
import urllib.request

CHUNK = 1 << 20


def read_manifest(path):
    entries = []
    with open(path, encoding="utf-8") as file:
        for number, line in enumerate(file, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 5:
                sys.exit(f"{path}:{number}: expected 5 fields (repo commit path sha256 size)")
            repo, commit, rel_path, sha256, size = parts
            if len(commit) != 40 or len(sha256) != 64 or ".." in rel_path.split("/"):
                sys.exit(f"{path}:{number}: a bad commit, SHA-256 or path")
            entries.append((repo, commit, rel_path, sha256, int(size)))
    return entries


def fetch(url, target, sha256, size):
    temp = target + ".part"
    digest = hashlib.sha256()
    seen = 0
    request = urllib.request.Request(url, headers={"User-Agent": "wattson-cognee-build"})
    with urllib.request.urlopen(request, timeout=120) as answer, open(temp, "wb") as out:
        while True:
            block = answer.read(CHUNK)
            if not block:
                break
            digest.update(block)
            seen += len(block)
            out.write(block)
    if seen != size or digest.hexdigest() != sha256:
        os.remove(temp)
        sys.exit(f"{url}: got {seen} bytes with SHA-256 {digest.hexdigest()}, expected {size} and {sha256}")
    os.chmod(temp, 0o644)
    os.replace(temp, target)


def main():
    manifest, cache = sys.argv[1], sys.argv[2]
    commits = {}
    for repo, commit, rel_path, sha256, size in read_manifest(manifest):
        if commits.setdefault(repo, commit) != commit:
            sys.exit(f"{repo}: two commits in {manifest}")
        folder = os.path.join(cache, "models--" + repo.replace("/", "--"))
        target = os.path.join(folder, "snapshots", commit, rel_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fetch(f"https://huggingface.co/{repo}/resolve/{commit}/{rel_path}", target, sha256, size)
        print(f"ok {repo}@{commit[:12]} {rel_path} ({size} bytes)")
    for repo, commit in commits.items():
        refs = os.path.join(cache, "models--" + repo.replace("/", "--"), "refs")
        os.makedirs(refs, exist_ok=True)
        with open(os.path.join(refs, "main"), "w", encoding="ascii") as file:
            file.write(commit)
    print(f"{len(commits)} repos in {cache}")


if __name__ == "__main__":
    main()
