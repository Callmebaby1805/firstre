# abbrev_cache_utils.py - Cache utilities for abbreviation expansion

import json
from pathlib import Path
from typing import Dict, Tuple
import hashlib

def load_cache(file: Path) -> Dict:
    """Load cache from a JSON file."""
    if file.exists():
        try:
            with file.open('r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[WARNING] Failed to load cache from {file}: {e}")
    return {}

def save_cache(cache: Dict, file: Path) -> None:
    """Save cache to a JSON file."""
    try:
        with file.open('w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except IOError as e:
        print(f"[ERROR] Failed to save cache to {file}: {e}")

def get_cache_key(prompt: str) -> str:
    """Generate a cache key from a prompt."""
    return hashlib.sha256(prompt.encode('utf-8')).hexdigest()

def update_context_cache(session_id: str, batch_number: int, content: str) -> Dict[str, str]:
    """Update context cache with content for a batch."""
    cache_file = Path(f"abbrev_context_cache_{session_id}.json")
    cache = load_cache(cache_file)
    cache[str(batch_number)] = content
    save_cache(cache, cache_file)
    return cache

def get_all_previous_content(cache: Dict[str, str], current_batch: int) -> Tuple[str, Dict[str, bool]]:
    """Retrieve previous content and acronym state from cache."""
    content = []
    acronym_state = {}
    for i in range(1, current_batch):
        if str(i) in cache:
            batch_content = cache[str(i)]
            if batch_content.startswith("ACRONYM_STATE:"):
                try:
                    state_str, text = batch_content.split("\n\n", 1)
                    state_json = state_str[len("ACRONYM_STATE:"):]
                    batch_state = json.loads(state_json)
                    if isinstance(batch_state, dict):
                        acronym_state.update(batch_state)
                    else:
                        print(f"[WARNING] Invalid acronym state format for batch {i}: {batch_state}")
                    content.append(text)
                except (ValueError, json.JSONDecodeError) as e:
                    print(f"[WARNING] Failed to parse ACRONYM_STATE for batch {i}: {e}")
                    content.append(batch_content)
            else:
                content.append(batch_content)
    return "\n\n".join(content), acronym_state