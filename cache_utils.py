import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ───────── LLM Response Cache Utilities ─────────

def load_cache(cache_file: Path = Path("llm_response_cache.json")) -> Dict[str, str]:
    """Load the LLM response cache from a JSON file."""
    if cache_file.exists():
        try:
            with open(cache_file, "r") as f:
                cache = json.load(f)
                print(f"[CACHE] Loaded cache from {cache_file} with {len(cache)} entries")
                return cache
        except Exception as e:
            print(f"[CACHE] Failed to load cache: {e}")
    print(f"[CACHE] No cache file found at {cache_file}, starting with empty cache")
    return {}


def save_cache(cache: Dict[str, str], cache_file: Path = Path("llm_response_cache.json")) -> None:
    """Save the LLM response cache to a JSON file."""
    try:
        with open(cache_file, "w") as f:
            json.dump(cache, f, indent=2)
        print(f"[CACHE] Saved cache to {cache_file} with {len(cache)} entries")
    except Exception as e:
        print(f"[CACHE] Failed to save cache: {e}")


def get_cache_key(prompt: str) -> str:
    """Generate a cache key by hashing the prompt."""
    import hashlib
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

# ───────── Context Cache Utilities ─────────

def update_context_cache(session_id: str, batch_num: int, content: str) -> Dict[str, str]:
    """Store full corrected text for each batch without truncation."""
    context_cache_file = Path(f"context_cache_{session_id}.json")
    if context_cache_file.exists():
        try:
            with open(context_cache_file, "r") as f:
                context_cache = json.load(f)
        except Exception as e:
            print(f"[CONTEXT CACHE] Error loading cache: {e}")
            context_cache = {}
    else:
        context_cache = {}

    context_cache[f"batch_{batch_num}"] = content

    try:
        with open(context_cache_file, "w") as f:
            json.dump(context_cache, f, indent=2)
        print(f"[CONTEXT CACHE] Updated context cache with full batch {batch_num}")
    except Exception as e:
        print(f"[CONTEXT CACHE] Error saving cache: {e}")

    return context_cache


def get_all_previous_content(context_cache: Dict[str, str], current_batch: int) -> str:
    """Get the complete content of all previous batches concatenated."""
    batch_keys = sorted(
        (k for k in context_cache if k.startswith("batch_")),
        key=lambda k: int(k.split("_")[1])
    )
    
    # Get all previous batches
    previous = [k for k in batch_keys if int(k.split("_")[1]) < current_batch]
    
    if previous:
        context = "\n\n".join(context_cache[k] for k in previous)
        print(f"[CONTEXT CACHE] Using {len(previous)} previous batches as context for batch {current_batch}")
        return context
    else:
        print(f"[CONTEXT CACHE] No previous batches found for context")
        return ""


def get_context_summary(context_cache: Dict[str, str], current_batch: int) -> str:
    """
    Get a summary of previous context for the current batch.
    This is a wrapper around get_all_previous_content for backward compatibility.
    
    Args:
        context_cache: Dictionary of batch contexts
        current_batch: Current batch number
        
    Returns:
        String with summary context from previous batches
    """
    return get_all_previous_content(context_cache, current_batch)

# ───────── Tracking Abbreviations ─────────

def load_abbreviation_cache(session_id: str) -> Dict[str, str]:
    """Load tracked abbreviations for the current document."""
    abbrev_cache_file = Path(f"abbrev_cache_{session_id}.json")
    if abbrev_cache_file.exists():
        try:
            with open(abbrev_cache_file, "r") as f:
                abbrev_cache = json.load(f)
                print(f"[ABBREV CACHE] Loaded abbreviations from {abbrev_cache_file} with {len(abbrev_cache)} entries")
                return abbrev_cache
        except Exception as e:
            print(f"[ABBREV CACHE] Error loading abbreviation cache: {e}")
    return {}


def save_abbreviation_cache(session_id: str, abbrev_cache: Dict[str, str]) -> None:
    """Save the abbreviation cache to a JSON file."""
    abbrev_cache_file = Path(f"abbrev_cache_{session_id}.json")
    try:
        with open(abbrev_cache_file, "w") as f:
            json.dump(abbrev_cache, f, indent=2)
        print(f"[ABBREV CACHE] Saved {len(abbrev_cache)} abbreviations to cache")
    except Exception as e:
        print(f"[ABBREV CACHE] Error saving abbreviation cache: {e}")


def extract_abbreviations_from_text(text: str) -> Dict[str, str]:
    """
    Extract abbreviations from text based on common patterns.
    Enhanced to catch more abbreviation patterns.
    """
    # Look for patterns like "Term (ABBR)" or "Term(ABBR)"
    pattern = r'([A-Za-z\s]+)\s*\(([A-Za-z0-9]+)\)'
    matches = re.findall(pattern, text)
    
    abbrevs = {}
    for term, abbr in matches:
        term = term.strip()
        if term and abbr:
            abbrevs[abbr] = term
    
    return abbrevs


def normalize_abbreviation(abbr: str) -> str:
    """Normalize abbreviation to handle case variations."""
    return abbr.upper()


def find_all_abbreviations(text: str) -> Dict[str, Set[str]]:
    """
    Find all potential abbreviations in the text, including variations.
    Returns a dictionary mapping normalized abbreviations to all their forms.
    """
    # Standard abbreviation pattern
    std_pattern = r'([A-Za-z\s]+)\s*\(([A-Za-z0-9]+)\)'
    std_matches = re.findall(std_pattern, text)
    
    # Standalone abbreviation pattern (looking for caps)
    standalone_pattern = r'\b([A-Z]{2,})\b'
    standalone_matches = re.findall(standalone_pattern, text)
    
    result = {}
    
    # Process standard abbreviations
    for term, abbr in std_matches:
        norm_abbr = normalize_abbreviation(abbr)
        if norm_abbr not in result:
            result[norm_abbr] = set()
        result[norm_abbr].add(abbr)
    
    # Add standalone abbreviations
    for abbr in standalone_matches:
        norm_abbr = normalize_abbreviation(abbr)
        if norm_abbr not in result:
            result[norm_abbr] = set()
        result[norm_abbr].add(abbr)
    
    return result


def scan_document_for_abbreviations(all_paras: List[str], session_id: str) -> Dict[str, str]:
    """
    Pre-scan the entire document to find all abbreviations and determine which need expansion.
    This helps identify which terms need to be expanded and which should use abbreviations.
    """
    full_text = "\n\n".join(all_paras)
    
    # First, extract defined abbreviations (Term (ABBR) format)
    abbrev_dict = extract_abbreviations_from_text(full_text)
    print(f"[ABBREV SCAN] Found {len(abbrev_dict)} explicitly defined abbreviations")
    
    # Get all variations of abbreviations
    all_abbrevs = find_all_abbreviations(full_text)
    
    # Find first occurrences of each abbreviation to determine if they need expansion
    first_occurrences = {}
    standalone_abbrevs = set()
    
    # Look for standalone abbreviations - these are likely candidates for expansion
    standalone_pattern = r'\b([A-Z]{2,})\b'
    for para in all_paras:
        for match in re.finditer(standalone_pattern, para):
            abbr = match.group(1)
            normalized = normalize_abbreviation(abbr)
            
            # If the abbreviation is not already defined but appears to be an acronym
            if normalized not in abbrev_dict and len(normalized) >= 2 and normalized.isupper():
                standalone_abbrevs.add(normalized)
                first_occurrence_point = match.start()
                
                # Check if this abbreviation appears before any defined version
                if normalized not in first_occurrences or first_occurrence_point < first_occurrences.get(normalized, float('inf')):
                    first_occurrences[normalized] = first_occurrence_point
                    print(f"[ABBREV SCAN] Found standalone abbreviation '{abbr}' that may need expansion")
    
    # Save the abbreviation cache
    save_abbreviation_cache(session_id, abbrev_dict)
    
    print(f"[ABBREV SCAN] Identified {len(standalone_abbrevs)} potential standalone abbreviations that may need expansion")
    return abbrev_dict


def get_abbreviation_context(session_id: str) -> str:
    """Generate context string with all tracked abbreviations."""
    abbrevs = load_abbreviation_cache(session_id)
    if not abbrevs:
        return ""
    
    context = "PREVIOUSLY DEFINED ABBREVIATIONS:\n"
    for abbr, term in abbrevs.items():
        context += f"- {term} ({abbr})\n"
    context += "\nIMPORTANT: For terms that have already been defined above, use ONLY the abbreviation in subsequent occurrences.\n"
    
    return context


def update_abbreviation_instructions(session_id: str, full_document_text: str) -> str:
    """
    Generate specific instructions for handling abbreviations based on the document context.
    This creates a more targeted approach than just listing abbreviations.
    """
    abbrevs = load_abbreviation_cache(session_id)
    
    # Identify all variations of abbreviations in the document
    all_abbrevs = find_all_abbreviations(full_document_text)
    
    instructions = "ABBREVIATION HANDLING INSTRUCTIONS:\n\n"
    
    # Add specific instructions for standalone abbreviations
    instructions += "STANDALONE ABBREVIATIONS:\n"
    instructions += "- When an abbreviation appears for the first time in the document without being previously defined, expand it.\n"
    instructions += "- For example, if 'GDP' appears without prior definition, expand it to 'Gross Domestic Product (GDP)'.\n"
    instructions += "- This applies to ALL common abbreviations and acronyms (UN, GHG, WHO, etc.).\n\n"
    
    # Add specific instructions for each abbreviation already defined
    instructions += "ALREADY DEFINED ABBREVIATIONS:\n"
    for abbr, term in abbrevs.items():
        # Find variations of this abbreviation
        norm_abbr = normalize_abbreviation(abbr)
        variations = all_abbrevs.get(norm_abbr, {abbr})
        
        # Add instruction for this abbreviation
        if len(variations) > 1:
            var_list = ", ".join(f'"{v}"' for v in variations)
            instructions += f"- The term \"{term}\" has been previously defined as {var_list}. " + \
                            f"Always use \"{abbr}\" for this term in subsequent occurrences.\n"
        else:
            instructions += f"- The term \"{term}\" has been previously defined as \"{abbr}\". " + \
                            f"Always use \"{abbr}\" for this term in subsequent occurrences.\n"
    
    # Add general instructions
    instructions += "\nIMPORTANT GUIDELINES:\n"
    instructions += "1. Do not redefine or expand any abbreviation that has already been defined previously.\n"
    instructions += "2. For terms like \"greenhouse gases\" that have already been defined as \"GHG\", always use just \"GHG\" in subsequent occurrences.\n"
    instructions += "3. When encountering a common abbreviation for the FIRST time, ALWAYS expand it, then use just the abbreviation afterwards.\n"
    
    return instructions