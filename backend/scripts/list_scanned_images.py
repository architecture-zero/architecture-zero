#!/usr/bin/env python3
"""Every container image this stack runs, once each: what the weekly scan reads.

WHY (2026-11-17). The repository scan (trivy, scan-type fs) reads files and
lockfiles. Nothing read the images the stack actually runs: the ones a compose
file pulls (a database, a cache, the edge) and the ones it builds. An
operating-system package CVE in any of them was invisible. The weekly image
scan (.github/workflows/image-scan.yml) scans what this prints, so the list is
read from the files instead of kept by hand: an image added to a compose file
or a Dockerfile is scanned from the next weekly run, and one removed stops
being scanned.

WHAT IS SCANNED, AND WHY BUILT IMAGES ARE BUILT FIRST (2026-11-21). Until
then every Dockerfile's FROM was scanned as pulled. That read the wrong
thing twice over: a build stage's base (node, to bundle a frontend) ships
nothing into the image that runs, yet its findings failed the scan; and a fix
a Dockerfile applies on top of its base (an OS package upgrade) was never
seen, so the scan could not say whether the image that runs is patched. A
Dockerfile a compose service builds is therefore built by the scan job and
the result scanned. So the list holds:
  - pull: each compose service's image:, except an image this repository
    builds itself. A service with a build: key names the tag it produces, and
    another service may run that same tag; neither is on a registry to pull.
  - build: each compose service's build: (its context, its Dockerfile, its
    target stage if it names one), once each however many services share it.
  - pull: every FROM of a Dockerfile no compose service builds (Dockerfile,
    Dockerfile.*, *.Dockerfile anywhere in the tree), except a stage an
    earlier FROM in the same file named (FROM deps AS builder) and scratch,
    which is no image at all. Such a file has no context this script can
    vouch for, so its bases are scanned as pulled rather than left out.
    node_modules, virtualenvs and nested checkouts are not walked: a git
    worktree inside the tree is a second copy of the same files, not a
    second image.

The same image named twice (nginx in a compose file and as a Dockerfile's
last stage; docker.io/library/nginx:1 and nginx:1) is printed once, and a
name with no tag is printed with the :latest a pull would take.

FAILS CLOSED. An image named through a variable (${TAG} in compose, an ARG in
a FROM) cannot be resolved from the file alone, and a list that dropped it
would scan everything but that image; a name with a character no image
reference has would reach a CI matrix as written. A build this checkout
cannot reproduce (a variable or a remote URL as its context, a context
outside the repository, an inline Dockerfile, a Dockerfile that is not
there) is the same. Each stops the listing with exit 2 and names the file.
Finding nothing to scan at all is exit 2 too: an empty list is a broken
reading, never a clean one.

  python backend/scripts/list_scanned_images.py          # one name per line
  python backend/scripts/list_scanned_images.py --json   # a JSON array of entries, for a CI matrix
  python backend/scripts/list_scanned_images.py --bases  # every Dockerfile's FROM bases, built or not
  python backend/scripts/list_scanned_images.py --root ../other-checkout

--bases is for a build host, not the scan: a host that builds these images
keeps the bases it pulled first and builds on them from then on, so a deploy
can pull each base by name before it builds (docker pull moves the local tag;
a build's own --pull does not). It reads Dockerfiles only, so it runs without
PyYAML.

Each --json entry has a name (what the scan job is called), and either an
image to pull ({"name", "image"}) or a build ({"name", "context",
"dockerfile"}, plus "target" when compose names a stage), its paths relative
to the repository root with forward slashes.
"""
import argparse
import json
import os
import re
import sys

try:
    import yaml
except ImportError:   # --bases reads Dockerfiles only; a bare build host can run it
    yaml = None

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

COMPOSE_FILE = re.compile(r"^(docker-)?compose[^/]*\.ya?ml$", re.IGNORECASE)
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__"}

# What an image reference is made of: registry host and port, path, tag and
# digest. Anything else (a space, a quote, a semicolon) is not an image, and
# the name goes on to a workflow matrix, so it stops the listing instead.
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
# A build stage's name, as Docker accepts one.
_STAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# A path that reaches the matrix: no shell, no quote, no whitespace.
_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")


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
    if yaml is None:
        raise ListingError("%s is a compose file and PyYAML is not installed" % path)
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    services = data.get("services") if isinstance(data, dict) else None
    if services is None and isinstance(data, dict):
        return {}   # a file of volumes or networks only pulls nothing
    if not isinstance(services, dict):
        raise ListingError("%s has no readable services: mapping" % path)
    return services


def _rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/")


def _build(root, path, name, build):
    """A compose build: key as (context, dockerfile, target): paths relative
    to the repository root, target "" when compose names no stage."""
    where = "%s (service %s)" % (path, name)
    if isinstance(build, str):
        context, dockerfile, target = build, None, None
    elif isinstance(build, dict):
        if "dockerfile_inline" in build:
            raise ListingError("%s builds from dockerfile_inline, which this script "
                               "cannot hand to a scan job" % where)
        context = build.get("context", ".")
        dockerfile = build.get("dockerfile")
        target = build.get("target")
    else:
        raise ListingError("%s has a build: that is neither a path nor a mapping" % where)
    for key, value in (("context", context), ("dockerfile", dockerfile), ("target", target)):
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise ListingError("%s has a build %s that is not a name" % (where, key))
        if "$" in value:
            raise ListingError("%s names its build %s through a variable (%s); it cannot be "
                               "resolved from the file, so it would go unscanned" % (where, key, value))
    if "://" in context or context.startswith("git@"):
        raise ListingError("%s builds from a remote context (%s), not from this checkout"
                           % (where, context))
    ctx = os.path.normpath(os.path.join(os.path.dirname(path), context))
    df = os.path.normpath(os.path.join(ctx, dockerfile or "Dockerfile"))
    for label, p in (("context", ctx), ("Dockerfile", df)):
        rel = os.path.relpath(p, root)
        if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
            raise ListingError("%s has a build %s outside the repository (%s)" % (where, label, p))
        if not _PATH.match(_rel(root, p)):
            raise ListingError("%s has a build %s path a scan job should not be handed (%r)"
                               % (where, label, _rel(root, p)))
    if not os.path.isdir(ctx):
        raise ListingError("%s builds from %s, which is not a directory here" % (where, _rel(root, ctx)))
    if not os.path.isfile(df):
        raise ListingError("%s builds %s, which is not there" % (where, _rel(root, df)))
    if target is not None and not _STAGE.match(target):
        raise ListingError("%s names a build target %r, which is not a stage name" % (where, target))
    return (_rel(root, ctx), _rel(root, df), target or "")


def read_compose(root, paths):
    """What the compose files pull and what they build: (images, builds)."""
    built_tags, pulled, builds = set(), [], set()
    for path in paths:
        for name, svc in _services(path).items():
            if not isinstance(svc, dict):
                continue
            image = svc.get("image")
            if image is not None and not isinstance(image, str):
                raise ListingError("%s: service %s has an image: that is not a name" % (path, name))
            if "build" in svc:
                builds.add(_build(root, path, name, svc["build"]))
                if image is not None:
                    built_tags.add(normalise(image))
                continue
            if image is not None:
                pulled.append((path, name, image))
    images = []
    for path, name, image in pulled:
        if normalise(image) in built_tags:
            continue
        images.append(_checked(image, "%s (service %s)" % (path, name)))
    return images, builds


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


def _read(root):
    images, builds = read_compose(root, compose_files(root))
    images = set(images)
    built_files = {df for _ctx, df, _target in builds}
    for path in dockerfiles(root):
        if _rel(root, path) in built_files:
            continue   # built and scanned as built; its bases are not what runs
        images.update(dockerfile_images(path))
    return sorted(images), sorted(builds)


def list_images(root=ROOT):
    """The images scanned as pulled: compose's own pulls and the bases of
    every Dockerfile no compose service builds."""
    return _read(root)[0]


def list_builds(root=ROOT):
    """The builds scanned as built: (context, dockerfile, target) each."""
    return _read(root)[1]


def list_bases(root=ROOT):
    """Every image a Dockerfile in the tree builds FROM, built or not."""
    bases = set()
    for path in dockerfiles(root):
        bases.update(dockerfile_images(path))
    if not bases:
        raise ListingError("no Dockerfile base found under %s - an empty list is a broken "
                           "reading, not a clean one" % root)
    return sorted(bases)


def build_name(dockerfile, target):
    return dockerfile + (" (stage %s)" % target if target else "")


def list_entries(root=ROOT):
    """The matrix: every pulled image and every build, once each."""
    images, builds = _read(root)
    entries = [{"name": img, "image": img} for img in images]
    for ctx, df, target in builds:
        entry = {"name": build_name(df, target), "context": ctx, "dockerfile": df}
        if target:
            entry["target"] = target
        entries.append(entry)
    if not entries:
        raise ListingError("nothing to scan under %s - an empty list is a broken "
                           "reading, not a clean one" % root)
    return entries


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Every container image this stack pulls or builds, once each.")
    out = parser.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="print a JSON array of matrix entries")
    out.add_argument("--bases", action="store_true",
                     help="print every Dockerfile's FROM bases, one per line (for a build host)")
    parser.add_argument("--root", default=ROOT, help="the repository to read (default: this one)")
    args = parser.parse_args(argv)
    errors = (ListingError, OSError, UnicodeDecodeError) + ((yaml.YAMLError,) if yaml else ())
    root = os.path.abspath(args.root)
    try:
        if args.bases:
            print("\n".join(list_bases(root)))
            return 0
        entries = list_entries(root)
    except errors as e:
        print("list_scanned_images: %s" % e, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(entries))
    else:
        print("\n".join(e["name"] for e in entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
