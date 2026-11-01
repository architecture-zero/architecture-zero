"""The extractor's tests import server.py and worker.py from the folder above."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
