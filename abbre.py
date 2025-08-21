from __future__ import annotations
import os
import time
import zipfile
import shutil
import argparse
import re
import json
import hashlib
from pathlib import Path
from typing import List, Tuple, Dict, Any
from lxml import etree
from copy import deepcopy
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.messages import AIMessage, HumanMessage
from abbriPrompt import ABBERI
from abbrev_cache_utils import (
    load_cache,
    save_cache,
    get_cache_key,
    update_context_cache,
    get_all_previous_content,
)
import asyncio
import anthropic  # NEW: For handling Anthropic-specific exceptions
import backoff  # NEW: For exponential backoff retries

# Constants
NS = {
    'w': "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    'xml': "http://www.w3.org/XML/1998/namespace"
}

FN_PLACEHOLDER = "§FOOTNOTE:{}§"
FN_PAT = re.compile(r"§FOOTNOTE:(\d+)§")
CTX_P = 10  # Number of initial paragraphs to prime context
CACHE_FILE = Path("llm_response_cache.json")
BATCH_SIZE = 4
# Regex to detect abbreviations like (GHG), (CO2), (IEA)
ABBREV_PAT = re.compile(r'\b[A-Za-z0-9\s]+(?=\s*\(([_A-Za-z0-9]+)\))')
Para = Tuple[etree._Element, str, List[Tuple[int, str]], List[Dict[str, Any]], bool]

# Rate limiting constants
TOKEN_LIMIT_PER_MINUTE = 40000
TOKENS_PER_CHAR = 0.38  # CHANGED: From 0.35 to 0.5 for conservative estimation (~2 chars per token)
TOKEN_BUFFER = 1000  # Safety buffer to avoid overshooting
RATE_LIMIT_THRESHOLD = 35000  # CHANGED: From 39000 to 30000 for larger buffer

# Utilities
def extract_docx(src: Path, dst: Path):
    with zipfile.ZipFile(src, 'r') as zf:
        zf.extractall(dst)

def rezip_docx(src_dir: Path, out: Path):
    if out.exists():
        out.unlink()
    shutil.make_archive(out.with_suffix(''), 'zip', src_dir)
    (out.with_suffix('').with_suffix('.zip')).rename(out)

# Text Extraction
def is_heading_or_title(p_elem: etree._Element, is_first_non_empty: bool) -> bool:
    """Identify if a paragraph is a heading or title."""
    style_elem = p_elem.find('.//w:pStyle', NS)
    if style_elem is not None:
        val = style_elem.get(f"{{{NS['w']}}}val", '').lower()
        if 'heading' in val or 'title' in val:
            return True
    if is_first_non_empty:  # First non-empty paragraph is likely the title
        return True
    if p_elem.find('.//w:b', NS) is not None:
        text = ''.join(t.text or '' for t in p_elem.findall('.//w:t', NS))
        if len(text.split()) <= 7:  # Short bolded text is often a heading
            return True
    return False

def is_table_paragraph(p_elem: etree._Element) -> bool:
    """Check if a paragraph is within a table."""
    current = p_elem
    while current is not None:
        if current.tag == f"{{{NS['w']}}}tbl":
            return True
        current = current.getparent()
    return False

def is_image_paragraph(p_elem: etree._Element) -> bool:
    """Check if a paragraph contains an image."""
    return p_elem.find('.//w:drawing', NS) is not None or p_elem.find('.//w:pict', NS) is not None

def is_toc_paragraph(p_elem: etree._Element, text: str, is_after_contents: bool) -> bool:
    """Check if a paragraph is part of the Table of Contents."""
    # Check style for TOC
    style_elem = p_elem.find('.//w:pStyle', NS)
    if style_elem is not None and 'toc' in style_elem.get(f"{{{NS['w']}}}val", '').lower():
        return True
    text_lower = text.strip().lower()
    if text_lower == 'contents':
        return True
    if is_after_contents:
        return bool(text.strip())  # Non-empty paragraphs after "Contents" are TOC
    return False

def is_section_heading(text: str) -> bool:
    """Check if a paragraph is a section heading (not a TOC entry)."""
    text_lower = text.strip().lower()
    section_headings = {'abbreviations', 'executive summary', 'introduction', 'conclusion'}
    return text_lower in section_headings

def is_footnote_or_citation(text: str) -> bool:
    return any([
        text.strip().startswith('[') and text.strip().endswith(']'),
        text.strip().isdigit() and len(text.strip()) <= 3,
        any(m in text for m in ['et al.', 'ibid', 'op. cit.', 'cf.', 'pp.', 'vol.']),
        any(text.strip().startswith(pref) for pref in ['[', '(', '^', '*']),
        any(c in text for c in '†‡§¹²³'),
        FN_PAT.search(text) is not None
    ])

def extract_paragraphs(doc_xml: Path) -> Tuple[etree._ElementTree, List[Para]]:
    tree = etree.parse(str(doc_xml))
    paras: List[Para] = []
    is_after_contents = False
    first_non_empty_found = False
    for p in tree.findall('.//w:p', NS):
        text_chunks: List[Tuple[int, str]] = []
        f_refs: List[Tuple[int, str]] = []
        special_elements: List[Dict[str, Any]] = []
        for idx, r in enumerate(p.findall('.//w:r', NS)):
            fn = r.find('w:footnoteReference', NS)
            if fn is not None:
                fid = fn.get(f"{{{NS['w']}}}id")
                f_refs.append((idx, fid))
                special_elements.append({'index': idx, 'type': 'footnoteReference', 'id': fid, 'formatting': deepcopy(r.find('w:rPr', NS))})
                continue
            t = r.find('w:t', NS)
            if t is not None and t.text:
                text_chunks.append((idx, t.text))
        if not text_chunks and not f_refs:
            continue
        combined = ''
        last = -1
        for idx, txt in sorted(text_chunks, key=lambda x: x[0]):
            for fi, fid in f_refs:
                if last < fi < idx:
                    combined += FN_PLACEHOLDER.format(fid)
            combined += txt
            last = idx
        for fi, fid in f_refs:
            if fi > last:
                combined += FN_PLACEHOLDER.format(fid)
        if is_footnote_or_citation(combined) and not f_refs:
            continue
        # Check for TOC, title, heading, table, or image
        has_text = bool(combined.strip())
        if has_text and not first_non_empty_found:
            first_non_empty_found = True
        is_toc = is_toc_paragraph(p, combined, is_after_contents)
        if combined.strip().lower() == 'contents':
            is_after_contents = True
        if is_after_contents and is_section_heading(combined):
            is_after_contents = False
        skip_processing = (
            is_toc or
            is_heading_or_title(p, not first_non_empty_found) or
            is_table_paragraph(p) or
            is_image_paragraph(p)
        )
        if f_refs:
            print(f"[DEBUG] Extracted paragraph with footnotes: '{combined}'")
        paras.append((p, combined, f_refs, special_elements, skip_processing))
    return tree, paras

# Helper function to set font to Calibri
def set_font_to_calibri(rPr: etree._Element):
    rFonts = rPr.find('.//w:rFonts', NS)
    if rFonts is None:
        rFonts = etree.SubElement(rPr, f"{{{NS['w']}}}rFonts")
    rFonts.set(f"{{{NS['w']}}}ascii", "Calibri")
    rFonts.set(f"{{{NS['w']}}}hAnsi", "Calibri")

def update_paragraph_structure(
    para_elem: etree._Element,
    corrected_text: str,
    special_elements: List[Dict[str, Any]]
) -> None:
    """Update paragraph XML structure with corrected text, preserving footnotes and bold/italic styles."""
    # Map footnote IDs to their formatting
    footnote_fmt: Dict[str, etree._Element] = {
        se['id']: se['formatting']
        for se in special_elements
        if se['type'] == 'footnoteReference'
    }
    
    # Extract original runs with their text and formatting to preserve bold/italic
    original_runs = []
    for r in para_elem.findall('.//w:r', NS):
        rPr = r.find('w:rPr', NS)
        t = r.find('w:t', NS)
        fn = r.find('w:footnoteReference', NS)
        if fn is not None:
            continue  # Skip footnote runs; handled separately
        if t is not None and t.text:
            is_bold = rPr is not None and rPr.find('w:b', NS) is not None
            is_italic = rPr is not None and rPr.find('w:i', NS) is not None
            original_runs.append({
                'text': t.text,
                'bold': is_bold,
                'italic': is_italic,
                'formatting': deepcopy(rPr) if rPr is not None else None
            })

    # Clear existing runs
    for r in list(para_elem.findall('.//w:r', NS)):
        r.getparent().remove(r)

    # Split corrected text into segments around footnotes
    segments: List[Tuple[str, str]] = []
    last = 0
    for m in FN_PAT.finditer(corrected_text):
        start, end = m.span()
        if start > last:
            segments.append(('text', corrected_text[last:start]))
        segments.append(('footnote', m.group(1)))
        last = end
    if last < len(corrected_text):
        segments.append(('text', corrected_text[last:]))

    # Assign bold/italic formatting by matching corrected text to original runs
    current_run_idx = 0
    current_run_offset = 0
    total_text_processed = 0

    for kind, val in segments:
        if kind == 'text':
            if not val:
                continue
            remaining_text = val
            while remaining_text:
                # Find the current original run that matches the start of remaining_text
                matched_run = None
                if current_run_idx < len(original_runs):
                    current_run = original_runs[current_run_idx]
                    original_text = current_run['text'][current_run_offset:]
                    # Check if the remaining_text starts with the current run's text
                    if remaining_text.startswith(original_text):
                        matched_run = current_run
                        text_to_use = original_text
                        current_run_idx += 1
                        current_run_offset = 0
                    else:
                        # Find the next run that matches the start of remaining_text
                        for i in range(current_run_idx, len(original_runs)):
                            run = original_runs[i]
                            if remaining_text.startswith(run['text']):
                                matched_run = run
                                text_to_use = run['text']
                                current_run_idx = i + 1
                                current_run_offset = 0
                                break
                        else:
                            # No matching run found; use the whole remaining_text with default formatting
                            text_to_use = remaining_text
                            matched_run = {'bold': False, 'italic': False, 'formatting': None}
                else:
                    # No more original runs; use default formatting
                    text_to_use = remaining_text
                    matched_run = {'bold': False, 'italic': False, 'formatting': None}

                # Create a new run
                r = etree.SubElement(para_elem, f"{{{NS['w']}}}r")
                rPr = etree.SubElement(r, f"{{{NS['w']}}}rPr")
                set_font_to_calibri(rPr)

                # Apply bold/italic if present in the matched run
                if matched_run['bold']:
                    etree.SubElement(rPr, f"{{{NS['w']}}}b")
                if matched_run['italic']:
                    etree.SubElement(rPr, f"{{{NS['w']}}}i")
                # Copy additional formatting if available
                if matched_run['formatting'] is not None:
                    for child in matched_run['formatting']:
                        if child.tag not in {f"{{{NS['w']}}}b", f"{{{NS['w']}}}i", f"{{{NS['w']}}}rFonts"}:
                            rPr.append(deepcopy(child))

                t = etree.SubElement(
                    r,
                    f"{{{NS['w']}}}t",
                    {f"{{{NS['xml']}}}space": "preserve"}
                )
                t.text = text_to_use
                remaining_text = remaining_text[len(text_to_use):]
                total_text_processed += len(text_to_use)

        else:
            # Handle footnote
            fid = val
            r = etree.SubElement(para_elem, f"{{{NS['w']}}}r")
            fmt = footnote_fmt.get(fid)
            if fmt is not None:
                r.append(deepcopy(fmt))
            fn = etree.SubElement(
                r,
                f"{{{NS['w']}}}footnoteReference",
                {f"{{{NS['w']}}}id": fid}
            )

def update_document(tree: etree._ElementTree, doc_xml: Path, new_paras: List[Para]) -> None:
    for para_elem, txt, f_refs, spec, skip in new_paras:
        if not skip:  # Only update structure for non-skipped paragraphs
            update_paragraph_structure(para_elem, txt, spec)
    try:
        tree.write(str(doc_xml), encoding='utf-8', xml_declaration=True, pretty_print=True)
        print(f"[DEBUG] Successfully wrote updated XML to {doc_xml}")
    except Exception as e:
        print(f"[ERROR] Failed to write updated XML to {doc_xml}: {e}")
        raise

class EnhancedChatMessageHistory(ChatMessageHistory):
    def add_message(self, message):
        super().add_message(message)
        return message
    def get_context_summary(self) -> str:
        msgs = self.messages[-3:]
        return '\n'.join(m.content for m in msgs)

def convo_chain(llm: ChatAnthropic):
    system_prompt = (
        "You are a precision-focused assistant for abbreviation expansion according to the ISAS Style Guide. "
        "Your task is to expand abbreviations on their first occurrence and use the abbreviated form thereafter. "
        "You MUST preserve footnote placeholders (e.g., §FOOTNOTE:1§) exactly as they appear in the input text. "
        "Do not alter or remove them."
    )
    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        MessagesPlaceholder("history"),
        ("human", "{input}")
    ])
    chain = prompt | llm
    store: Dict[str, BaseChatMessageHistory] = {}
    def hist(sid: str) -> BaseChatMessageHistory:
        if sid not in store:
            store[sid] = EnhancedChatMessageHistory()
        return store[sid]
    return RunnableWithMessageHistory(
        chain,
        hist,
        input_messages_key='input',
        history_messages_key='history'
    ), store

def split_paragraph(text: str) -> List[Tuple[str, str, bool, bool]]:
    parts = []
    last_pos = 0
    for m in FN_PAT.finditer(text):
        start, end = m.span()
        if start > last_pos:
            segment = text[last_pos:start]
            space_after = text[start-1].isspace() if start > 0 else False
            parts.append(('text', segment, False, space_after))
        space_before = text[start-1].isspace() if start > 0 else False
        space_after = text[end].isspace() if end < len(text) else False
        parts.append(('footnote', m.group(0), space_before, space_after))
        last_pos = end
    if last_pos < len(text):
        segment = text[last_pos:]
        space_before = text[last_pos-1].isspace() if last_pos > 0 else False
        parts.append(('text', segment, space_before, False))
    return parts

def parse_abbreviations(text: str) -> set:
    """Parse abbreviations from text, detecting any (ABBR) pattern."""
    new_abbreviations = set()
    for match in ABBREV_PAT.finditer(text):
        abbrev = match.group(1)
        if abbrev:  # Ensure abbreviation is captured
            new_abbreviations.add(abbrev)
    return new_abbreviations

class AbbreviationExpander:
    def __init__(self, llm: ChatAnthropic):
        self.llm = llm
        self.token_count = 0
        self.last_reset_time = time.time()

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count based on character length."""
        return int(len(text) * TOKENS_PER_CHAR)

    async def manage_rate_limit(self, prompt: str):
        """Check and manage token rate limit, awaiting 65 seconds if token count reaches or exceeds 35,000."""
        estimated_tokens = self.estimate_tokens(prompt)
        print(f"[DEBUG] Estimated tokens for prompt: {estimated_tokens}")
        print(f"[DEBUG] Current token count before adding: {self.token_count}")

        # Check if adding the estimated tokens would reach or exceed 35,000 tokens
        if self.token_count + estimated_tokens >= RATE_LIMIT_THRESHOLD:
            print(f"[INFO] Token count ({self.token_count} + {estimated_tokens} >= {RATE_LIMIT_THRESHOLD}). Awaiting 65 seconds.")
            await asyncio.sleep(65)
            self.token_count = 0
            self.last_reset_time = time.time()
            print(f"[DEBUG] Reset token count to 0 at {self.last_reset_time}")

        self.token_count += estimated_tokens
        print(f"[DEBUG] Updated token count: {self.token_count}")

    def normalize_variants(self, text: str, variant_map: dict) -> str:
        """Normalize text by replacing all variants with their canonical keys, respecting quotes and footnotes."""
        normalized_text = text
        # Regular expression to match quoted text (single or double quotes)
        quote_pat = re.compile(r'(["\'].*?["\'])')
        # Split text into segments: quoted and non-quoted
        segments = []
        last_pos = 0
        for match in quote_pat.finditer(normalized_text):
            start, end = match.span()
            if start > last_pos:
                segments.append(('text', normalized_text[last_pos:start]))
            segments.append(('quote', match.group(0)))
            last_pos = end
        if last_pos < len(normalized_text):
            segments.append(('text', normalized_text[last_pos:]))

        # Process only non-quoted segments
        result = []
        for seg_type, seg_text in segments:
            if seg_type == 'quote':
                result.append(seg_text)  # Preserve quoted text unchanged
                continue
            # Replace variants in non-quoted text, ensuring footnote placeholders are not affected
            temp_text = seg_text
            for key, variants in variant_map.items():
                for variant in sorted(variants, key=len, reverse=True):  # Longer variants first
                    # Use word boundaries to avoid partial matches
                    pattern = r'\b' + re.escape(variant) + r'\b'
                    # Ensure replacement doesn't occur within footnote placeholders
                    if not FN_PAT.search(variant):
                        temp_text = re.sub(pattern, key, temp_text)
            result.append(temp_text)
        
        normalized_text = ''.join(result)
        print(f"[DEBUG] Normalized text preview: {normalized_text[:500]}...")
        return normalized_text

    # NEW: Method to invoke LLM with retry logic for rate limit errors
    @backoff.on_exception(
        backoff.expo,
        anthropic.RateLimitError,
        max_tries=5,
        factor=65  # Wait 60s, 120s, 240s
    )
    async def invoke_llm(self, chain, input_data, config):
        """Invoke LLM with retry logic for rate limit errors."""
        try:
            resp = await chain.ainvoke(input_data, config=config)
            return resp
        except anthropic.RateLimitError as e:
            print(f"[ERROR] Rate limit hit: {e}. Retrying after backoff...")
            raise

    async def process(self, input_path: str, output_path: str, variant_map_path: str = "variant_map.json"):
        llm_cache_file = Path("llm_response_cache.json")
        if llm_cache_file.exists():
            llm_cache_file.unlink()
            print(f"[INFO] Deleted existing {llm_cache_file}")
        for context_cache_file in Path.cwd().glob("abbrev_context_cache_*.json"):
            context_cache_file.unlink()
            print(f"[INFO] Deleted existing {context_cache_file}")
        
        # Load variant map
        if not Path(variant_map_path).is_file():
            raise FileNotFoundError(f"Variant map JSON not found: {variant_map_path}")
        with open(variant_map_path, encoding="utf-8") as f:
            variant_map = json.load(f)
        
        tmp = Path('_extract')
        tmp.mkdir(exist_ok=True)
        extract_docx(Path(input_path), tmp)
        doc_xml = tmp / 'word' / 'document.xml'
        tree, paras = extract_paragraphs(doc_xml)

        # Normalize variants in eligible paragraphs
        normalized_paras = []
        for para in paras:
            para_elem, text, f_refs, spec, skip = para
            normalized_text = text if skip else self.normalize_variants(text, variant_map)
            normalized_paras.append((para_elem, normalized_text, f_refs, spec, skip))
        
        chain, history_store = convo_chain(self.llm)
        session_id = f'doc_{Path(input_path).stem}'
        
        # Use only eligible non-skipped paragraphs for context
        ctx_paras = [p[1] for p in normalized_paras if not p[4]][:CTX_P]
        ctx = '\n\n'.join(ctx_paras)
        await self.manage_rate_limit(ctx)
        await self.invoke_llm(  # CHANGED: Use invoke_llm instead of chain.ainvoke
            chain,
            {'input': f'Context for abbreviation expansion:\n{ctx}'},
            {'configurable': {'session_id': session_id}}
        )

        cache = load_cache(CACHE_FILE)
        context_cache = {}
        corrected: List[Para] = []
        
        for i in range(0, len(normalized_paras), BATCH_SIZE):
            bn = i // BATCH_SIZE + 1
            batch = normalized_paras[i:i+BATCH_SIZE]
            
            text_segments = []
            segment_counts = []
            placeholder_lists = []
            batch_elements = []
            for para in batch:
                para_elem, text, f_refs, spec, skip = para
                if skip:
                    corrected.append((para_elem, text, f_refs, spec, True))
                    print(f"[DEBUG] Batch {bn} - Skipped paragraph (TOC/title/heading/table/image): '{text[:30]}...'")
                    continue
                batch_elements.append(para)
                parts = split_paragraph(text)
                para_segments = [part[1] for part in parts if part[0] == 'text' and part[1].strip()]
                para_placeholders = [part[1] for part in parts if part[0] == 'footnote']
                print(f"[DEBUG] Batch {bn} - Original paragraph: '{text}'")
                print(f"[DEBUG] Batch {bn} - Text segments: {para_segments}")
                print(f"[DEBUG] Batch {bn} - Footnotes: {para_placeholders}")
                text_segments.extend(para_segments)
                segment_counts.append(len(para_segments))
                placeholder_lists.append(para_placeholders)
            
            if not text_segments:
                continue  # Skip empty batches
            
            segments_to_correct = "\n---\n".join(text_segments)
            
            prev_content, acronym_state = get_all_previous_content(context_cache, bn)
            state_json = json.dumps(acronym_state, sort_keys=True)
            full_context = ""
            if prev_content:
                full_context += "PREVIOUS CORRECTED CONTENT:\n" + prev_content + "\n\n"
            full_context += f"CURRENT ACRONYM STATE:\n{state_json}\n\n"
            
            prompt = (
                full_context +
                ABBERI +
                "\nExpand abbreviations in the following text segments according to the ISAS Style Guide. "
                "Each segment is separated by '---'. Preserve the separators in your response. "
                "Check the CURRENT ACRONYM STATE before expanding: only expand an abbreviation if its key is not in the state (i.e., first occurrence). "
                "For subsequent occurrences, use the abbreviation alone (e.g., 'GHG' instead of 'Greenhouse Gas (GHG)').\n\n" +
                segments_to_correct
            )
            
            key = get_cache_key(prompt)
            if key in cache:
                out = cache[key]
            else:
                await self.manage_rate_limit(prompt)
                resp = await self.invoke_llm(  # CHANGED: Use invoke_llm instead of chain.ainvoke
                    chain,
                    {'input': prompt},
                    {'configurable': {'session_id': session_id}}
                )
                out = resp.content.strip()
                # Clean response to remove any commentary and empty segments
                segment_pattern = re.compile(r'([^\n].*?)(?=\n---\n|$)', re.DOTALL)
                corrected_segments = [m.group(1).strip() for m in segment_pattern.finditer(out + '\n---\n') if m.group(1).strip()]
                # Remove --- prefixes and standalone --- segments
                corrected_segments = [re.sub(r'^---\n?', '', seg).strip() for seg in corrected_segments if seg.strip() and seg.strip() != '---']
                out = '\n---\n'.join(corrected_segments)
                cache[key] = out
                save_cache(cache, CACHE_FILE)
            
            corrected_segments = out.split("\n---\n")
            print(f"[DEBUG] Batch {bn} - Corrected segments from LLM: {corrected_segments}")
            
            if len(corrected_segments) != len(text_segments):
                print(f"[ERROR] Batch {bn} - Segment mismatch: expected {len(text_segments)} segments, got {len(corrected_segments)}")
                print(f"[DEBUG] Input segments: {text_segments}")
                print(f"[DEBUG] Output segments: {corrected_segments}")
                corrected_segments = corrected_segments[:len(text_segments)]  # Truncate to expected length
            
            corrected_paras = []
            seg_idx = 0
            for i, (para_elem, original_text, f_refs, spec, _) in enumerate(batch_elements):
                num_segments = segment_counts[i]
                para_placeholders = placeholder_lists[i]
                corrected_text = ""
                part_idx = 0
                print(f"[DEBUG] Batch {bn} - Paragraph {i} - Expected segments: {num_segments}, Available corrected segments: {len(corrected_segments) - seg_idx}")
                for part in split_paragraph(original_text):
                    if part[0] == 'text' and part[1].strip():
                        if seg_idx + part_idx < len(corrected_segments) and part_idx < num_segments:
                            corrected_text += corrected_segments[seg_idx + part_idx]
                            part_idx += 1
                        else:
                            print(f"[WARNING] Batch {bn} - Paragraph {i} - Missing corrected segment {part_idx}, using original: '{part[1]}'")
                            corrected_text += part[1]
                        if part[3]:
                            corrected_text += " "
                    elif part[0] == 'footnote':
                        if part[2]:
                            corrected_text += " "
                        corrected_text += part[1]
                        if part[3]:
                            corrected_text += " "
                print(f"[DEBUG] Batch {bn} - Reassembled corrected paragraph {i}: '{corrected_text}'")
                corrected_paras.append(corrected_text)
                seg_idx += num_segments
    
            # Clean corrected_paras to remove any --- for both document and cache
            cleaned_paras = [re.sub(r'^---\n?', '', para).strip() for para in corrected_paras]
            for j, (para_elem, _, f_refs, spec, _) in enumerate(batch_elements):
                corrected.append((para_elem, cleaned_paras[j], f_refs, spec, False))
            
            reassembled_text = "\n\n".join(cleaned_paras)
            new_abbreviations = parse_abbreviations(reassembled_text)
            for abbr in new_abbreviations:
                if abbr not in acronym_state:
                    acronym_state[abbr] = True
            state_json = json.dumps(acronym_state, sort_keys=True)
            cache_content = f"ACRONYM_STATE:{state_json}\n\n{reassembled_text}"
            context_cache = update_context_cache(session_id, bn, cache_content)
            print(f"[DEBUG] Batch {bn} - Updated acronym state: {acronym_state}")
        
        update_document(tree, doc_xml, corrected)
        rezip_docx(tmp, Path(output_path))
        shutil.rmtree(tmp)

async def main():
    load_dotenv()
    input_path = "SS.docx"
    output_path = "abbrev_expandedPart.docx"
    llm = ChatAnthropic(
        model="claude-3-5-haiku-20241022",
        # model="claude-sonnet-4-20250514",
        temperature=0.1,
        api_key=os.getenv("ANTHROPIC_API_KEY")
    )
    expander = AbbreviationExpander(llm)
    await expander.process(input_path, output_path)
    print(f"✅ Abbreviation expansion complete. Output saved to: {output_path}")

if __name__ == "__main__":
    asyncio.run(main())