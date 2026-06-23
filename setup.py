"""
One-time setup: downloads WordNet and CMU Pronouncing Dictionary to the
local data/nltk_data directory so Lingua works fully offline.

Run once:  python setup.py
"""
import sys
from pathlib import Path

nltk_data_dir = Path(__file__).parent / "data" / "nltk_data"
nltk_data_dir.mkdir(parents=True, exist_ok=True)

try:
    import nltk
except ImportError:
    print("ERROR: nltk is not installed.")
    print("Run: pip install -r requirements.txt")
    sys.exit(1)

if str(nltk_data_dir) not in nltk.data.path:
    nltk.data.path.insert(0, str(nltk_data_dir))

corpora = [
    ("wordnet", "WordNet (definitions, synonyms, antonyms, hypernyms)"),
    ("cmudict",  "CMU Pronouncing Dictionary (phonetic transcription)"),
    ("omw-1.4",  "Open Multilingual WordNet (extended coverage)"),
]

print()
for corpus, desc in corpora:
    print(f"  Downloading {desc}...", end=" ", flush=True)
    nltk.download(corpus, download_dir=str(nltk_data_dir), quiet=True)
    print("done")

print()
print("Setup complete. Run 'run.cmd' (or: python main.py) to start Lingua.")
print()
