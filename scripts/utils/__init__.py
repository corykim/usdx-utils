"""Import-only modules shared by the scripts in scripts/.

Anything here holds functions or constants and nothing else -- no argument
parsing, no main(), nothing that runs on import. A file that does something
when you run it is a script and belongs one level up.

A script started as `uv run scripts/<name>.py` gets scripts/ as sys.path[0],
so `from utils import audio_lengths` resolves with no sys.path juggling. The
modules do not import each other, so importing one never drags in the rest.
"""

from . import audio_lengths, song_folders

__all__ = ["audio_lengths", "song_folders"]
