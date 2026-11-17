#!/usr/bin/env python3
"""Every container image this repository pulls or builds FROM, once each.

WHY (2026-11-17). The repository scan (trivy, scan-type fs) reads files and
lockfiles. Nothing read the images the stack actually runs: the ones a compose
file pulls (a database, a cache, the edge) and the base image every Dockerfile
builds on. An operating-system package CVE in any of them was invisible. The
weekly image scan (.github/workflows/image-scan.yml) scans what this prints,
so the list is read from the files instead of kept by hand: an image added to
a compose file or a Dockerfile is scanned from the next weekly run, and one
removed stops being scanned.

WHAT IT READS
  - every compose file at the repository root (docker-compose*.yml,
    compose.yml, and their .yaml spellings): each service's image:, except
    an image this repository builds itself. A service with a build: key names
    the tag it produces, and another service may run that same tag; neither
    is on a registry to pull.
  - every Dockerfile in the tree (Dockerfile, Dockerfile.*, *.Dockerfile):
    each FROM, except a stage an earlier FROM in the same file named
    (FROM deps AS builder) and scratch, which is no image at all.
    node_modules, virtualenvs and nested checkouts are not walked: a git
    worktree inside the tree is a second copy of the same files, not a
    second image.

The same image named twice (nginx in a compose file and as a Dockerfile's
last stage; docker.io/library/nginx:1 and nginx:1) is printed once, and a
name with no tag is printed with the :latest a pull would take.

FAILS CLOSED. An image named through a variable (${TAG} in compose, an ARG in
a FROM) cannot be resolved from the file alone, and a list that dropped it
would scan everything but that image; a name with a character no image
reference has would reach a CI matrix as written. Either stops the listing
with exit 2 and names the file. Finding no image at all is exit 2 too: an
empty list is a broken reading, never a clean one.

  python backend/scripts/list_scanned_images.py          # one image per line
  python backend/scripts/list_scanned_images.py --json   # a JSON array, for a CI matrix
  python backend/scripts/list_scanned_images.py --root ../other-checkout
"""
import argparse
import json
import os
import re
import sys

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

COMPOSE_FILE = re.compile(r"^(docker-)?compose[^/]*\.ya?ml$", re.IGNORECASE)
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__"}

# What an image reference is made of: registry host and port, path, tag and
# digest. Anything else (a space, a quote, a semicolon) is not an image, and
# the name goes on to a workflow matrix, so it stops the listing instead.
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")


class ListingError(Exception):
    """The files name an image this script cannot list truthfully."""


def is_dockerfile(name):
    n = name.lower()
    if n.endswith(".dockerignore"):
        return False
    return n == "dockerfile" or n.startswith("dockerfile.") or n.endswith(".dockerfile")


def normalise(ref):
    """One spelling per image: Docker Hub's implied registry and library/
    path dropped, the implied :latest written out."""
    ref = ref.strip()
    for prefix in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
            break
    first = ref.split("/", 1)[0]
    on_hub = "/" not in ref or not ("." in first or ":" in first or first == "localhost")
    if on_hub and ref.startswith("library/"):
        ref = ref[len("library/"):]
    if "@" not in ref and ":" not in ref.rsplit("/", 1)[-1]:
        ref += ":latest"
    return ref


def _checked(ref, where):
    if "$" in ref:
        raise ListingError("%s names an image through a variable (%s); it cannot be "
                           "resolved from the file, so it would go unscanned" % (where, ref))
    if not _REFERENCE.match(ref):
        raise ListingError("%s names %r, which is not an image reference" % (where, ref))
    return normalise(ref)


def compose_files(root):
    return sorted(os.path.join(root, f) for f in os.listdir(root)
                  if COMPOSE_FILE.match(f) and os.path.isfile(os.path.join(root, f)))


def _services(path):
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    services = data.get("services") if isinstance(data, dict) else None
    if services is None and isinstance(data, dict):
        return {}   # a file of volumes or networks only pulls nothing
    if not isinstance(services, dict):
        raise ListingError("%s has no readable services: mapping" % path)
    return services


def compose_images(paths):
    """The images the compose files pull: every service's image: that no
    service with a build: key produces (in any of the files)."""
    built, pulled = set(), []
    for path in paths:
        for name, svc in _services(path).items():
            if not isinstance(svc, dict):
                continue
            image = svc.get("image")
            if image is None:
                continue
            if not isinstance(image, str):
                raise ListingError("%s: service %s has an image: that is not a name" % (path, name))
            if "build" in svc:
                built.add(normalise(image))
                continue
            pulled.append((path, name, image))
    out = []
    for path, name, image in pulled:
        if normalise(image) in built:
            continue
        out.append(_checked(image, "%s (service %s)" % (path, name)))
    return out


def _instructions(text):
    """The file's instructions, continuation lines joined, comments dropped."""
    out, buf = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append(buf + line)
        buf = ""
    if buf.strip():
        out.append(buf)
    return out


def dockerfile_images(path):
    """The images a Dockerfile's FROM lines pull: not a stage the file named
    earlier, not scratch."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    stages, out = set(), []
    for line in _instructions(text):
        parts = line.split()
        if parts[0].upper() != "FROM":
            continue
        args = [p for p in parts[1:] if not p.startswith("--")]
        if not args:
            raise ListingError("%s has a FROM with no image: %s" % (path, line))
        image = args[0]
        if image.lower() != "scratch" and image.lower() not in stages:
            out.append(_checked(image, path))
        if len(args) >= 3 and args[1].upper() == "AS":
            stages.add(args[2].lower())
    return out


def dockerfiles(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIRS and not os.path.exists(os.path.join(dirpath, d, ".git")))
        found.extend(os.path.join(dirpath, f) for f in sorted(filenames) if is_dockerfile(f))
    return found


def list_images(root=ROOT):
    images = set(compose_images(compose_files(root)))
    for path in dockerfiles(root):
        images.update(dockerfile_images(path))
    if not images:
        raise ListingError("no image found under %s - an empty list is a broken "
                           "reading, not a clean one" % root)
    return sorted(images)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Every container image this repository pulls or builds FROM, once each.")
    parser.add_argument("--json", action="store_true", help="print a JSON array")
    parser.add_argument("--root", default=ROOT, help="the repository to read (default: this one)")
    args = parser.parse_args(argv)
    try:
        images = list_images(args.root)
    except (ListingError, OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        print("list_scanned_images: %s" % e, file=sys.stderr)
        return 2
    print(json.dumps(images) if args.json else "\n".join(images))
    return 0


if __name__ == "__main__":
    sys.exit(main())
