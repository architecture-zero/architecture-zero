"""Version floors for advisories the dependency scan cannot see.

pip-audit reads the global advisory database. Some projects publish an
advisory only in their own GitHub repository (a repository advisory); it never
reaches that database or OSV, so a scan of these pins passes while a pin sits
in the affected range. Each floor below names its advisory. Raise a floor in
the same commit that moves its pin, and keep this file one text on every
surface that carries it.

- fastapi GHSA-f56c-v883-vx8r (high, published 2026-11-17): denial of service
  through quadratic processing of repeated query, header and form parameters,
  on every route that takes them. Fixed in 0.163.2.
"""
import pathlib
import re

import fastapi

FLOORS = {
    # package: (lowest safe version, advisory)
    "fastapi": ("0.163.2", "GHSA-f56c-v883-vx8r"),
}

REQUIREMENTS = pathlib.Path(__file__).resolve().parents[1] / "requirements.txt"


def _version(text):
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def _pins():
    pins = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_.-]+)(\[[^\]]*\])?==([0-9][^\s;#]*)", line)
        if m:
            pins[m.group(1).lower()] = m.group(3)
    return pins


def test_pins_meet_advisory_floors():
    pins = _pins()
    for package, (floor, advisory) in FLOORS.items():
        assert package in pins, f"{package} is not pinned with == in {REQUIREMENTS.name}"
        assert _version(pins[package]) >= _version(floor), (
            f"{package}=={pins[package]} is below {floor} ({advisory})")


def test_installed_fastapi_meets_its_floor():
    # The pin is what ships; this proves the image the suite runs in took it.
    floor, advisory = FLOORS["fastapi"]
    assert _version(fastapi.__version__) >= _version(floor), (
        f"installed fastapi {fastapi.__version__} is below {floor} ({advisory})")
