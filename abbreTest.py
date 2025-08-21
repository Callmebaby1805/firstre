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
import anthropic

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
TOKENS_PER_CHAR = 0.35
TOKEN_THRESHOLD = 39000
MAX_PROMPT_TOKENS = 190000
HISTORY_TOKEN_LIMIT = 150000

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
        last_idx = -1
        sorted_chunks = sorted(text_chunks, key=lambda x: x[0])
        sorted_f_refs = sorted(f_refs, key=lambda x: x[0])
        chunk_idx = 0
        ref_idx = 0
        current_pos = 0
        while chunk_idx < len(sorted_chunks) or ref_idx < len(sorted_f_refs):
            next_chunk_idx = sorted_chunks[chunk_idx][0] if chunk_idx < len(sorted_chunks) else float('inf')
            next_ref_idx = sorted_f_refs[ref_idx][0] if ref_idx < len(sorted_f_refs) else float('inf')
            if next_ref_idx < next_chunk_idx:
                if next_ref_idx > current_pos:
                    combined += ' ' * (next_ref_idx - current_pos)
                combined += FN_PLACEHOLDER.format(sorted_f_refs[ref_idx][1])
                current_pos = next_ref_idx + 1
                ref_idx += 1
            else:
                if next_chunk_idx > current_pos:
                    combined += ' ' * (next_chunk_idx - current_pos)
                combined += sorted_chunks[chunk_idx][1]
                current_pos = next_chunk_idx + 1
                chunk_idx += 1
        if is_footnote_or_citation(combined) and not f_refs:
            continue
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
        print(f"[DEBUG] Extracted paragraph text: '{combined[:100]}...' (length: {len(combined)})")
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
    footnote_fmt: Dict[str, etree._Element] = {
        se['id']: se['formatting']
        for se in special_elements
        if se['type'] == 'footnoteReference'
    }
    
    original_runs = []
    for r in para_elem.findall('.//w:r', NS):
        rPr = r.find('w:rPr', NS)
        t = r.find('w:t', NS)
        fn = r.find('w:footnoteReference', NS)
        if fn is not None:
            continue
        if t is not None and t.text:
            is_bold = rPr is not None and rPr.find('w:b', NS) is not None
            is_italic = rPr is not None and rPr.find('w:i', NS) is not None
            original_runs.append({
                'text': t.text,
                'bold': is_bold,
                'italic': is_italic,
                'formatting': deepcopy(rPr) if rPr is not None else None
            })

    for r in list(para_elem.findall('.//w:r', NS)):
        r.getparent().remove(r)

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

    current_run_idx = 0
    current_run_offset = 0
    for kind, val in segments:
        if kind == 'text':
            if not val:
                continue
            remaining_text = val
            while remaining_text:
                matched_run = None
                if current_run_idx < len(original_runs):
                    current_run = original_runs[current_run_idx]
                    original_text = current_run['text'][current_run_offset:]
                    if remaining_text.startswith(original_text):
                        matched_run = current_run
                        text_to_use = original_text
                        current_run_idx += 1
                        current_run_offset = 0
                    else:
                        for i in range(current_run_idx, len(original_runs)):
                            run = original_runs[i]
                            if remaining_text.startswith(run['text']):
                                matched_run = run
                                text_to_use = run['text']
                                current_run_idx = i + 1
                                current_run_offset = 0
                                break
                if matched_run is None:
                    text_to_use = remaining_text
                    matched_run = {'bold': False, 'italic': False, 'formatting': None}

                r = etree.SubElement(para_elem, f"{{{NS['w']}}}r")
                rPr = etree.SubElement(r, f"{{{NS['w']}}}rPr")
                set_font_to_calibri(rPr)
                if matched_run['bold']:
                    etree.SubElement(rPr, f"{{{NS['w']}}}b")
                if matched_run['italic']:
                    etree.SubElement(rPr, f"{{{NS['w']}}}i")
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
        else:
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
        if not skip:
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

    def trim_history(self, token_limit: int, tokens_per_char: float = TOKENS_PER_CHAR):
        """Trim history to stay within token limit."""
        total_tokens = 0
        trimmed_messages = []
        for msg in reversed(self.messages):
            msg_tokens = int(len(msg.content) * tokens_per_char)
            if total_tokens + msg_tokens > token_limit:
                break
            trimmed_messages.append(msg)
            total_tokens += msg_tokens
        self.messages = list(reversed(trimmed_messages))
        print(f"[DEBUG] Trimmed history to {len(self.messages)} messages, ~{total_tokens} tokens")

def convo_chain(llm: ChatAnthropic):
    system_prompt = (
        "You are a precision-focused assistant for abbreviation expansion according to the ISAS Style Guide. "
        "Your task is to expand abbreviations on their first occurrence and use the abbreviated form "
        "thereafter. You MUST preserve footnote placeholders (e.g., §FOOTNOTE:1§) exactly as they appear in the input text. "
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

def split_paragraph(text: str) -> List[Tuple[str, str, bool, bool, bool]]:
    parts = []
    last_pos = 0
    for m in FN_PAT.finditer(text):
        start, end = m.span()
        if start > last_pos:
            segment = text[last_pos:start]
            space_after = text[start-1].isspace() if start > 0 else False
            is_empty = not segment.strip()
            parts.append(('text', segment, False, space_after, is_empty))
        space_before = text[start-1].isspace() if start > 0 else False
        space_after = text[end].isspace() if end < len(text) else False
        parts.append(('footnote', m.group(0), space_before, space_after, False))
        last_pos = end
    if last_pos < len(text):
        segment = text[last_pos:]
        space_before = text[last_pos-1].isspace() if last_pos > 0 else False
        is_empty = not segment.strip()
        parts.append(('text', segment, space_before, False, is_empty))
    return parts

def parse_abbreviations(text: str) -> set:
    """Parse abbreviations from text, detecting any (ABBR) pattern."""
    new_abbreviations = set()
    for match in ABBREV_PAT.finditer(text):
        abbrev = match.group(1)
        if abbrev:
            new_abbreviations.add(abbrev)
    return new_abbreviations

class AbbreviationExpander:
    def __init__(self, llm: ChatAnthropic):
        self.llm = llm
        self.total_token_count = 0
        self.minute_start_time = time.time()
        self.last_response_headers = {
            'x-ratelimit-tokens-remaining': TOKEN_LIMIT_PER_MINUTE,
            'x-ratelimit-reset-tokens': 60
        }
        self.global_acronym_state = {}  # Global state to track acronyms across batches

    def estimate_tokens(self, text: str) -> int:
        """Estimate total tokens (input + output) for a given text."""
        input_tokens = int(len(text) * TOKENS_PER_CHAR)
        total_tokens = int(2.3 * input_tokens)
        print(f"[DEBUG] Estimated input tokens: {input_tokens}, Total tokens (input + output): {total_tokens}")
        return total_tokens

    def manage_rate_limit(self, prompt: str):
        """Manage token rate limit by tracking usage and waiting if necessary."""
        current_time = time.time()
        elapsed_time = current_time - self.minute_start_time

        if elapsed_time >= 60:
            print(f"[DEBUG] Minute elapsed ({elapsed_time:.2f}s). Resetting token count at {current_time}")
            self.total_token_count = 0
            self.minute_start_time = current_time

        estimated_tokens = self.estimate_tokens(prompt)
        
        if self.total_token_count + estimated_tokens > TOKEN_THRESHOLD:
            wait_time = float(self.last_response_headers.get('x-ratelimit-reset-tokens', 62))
            print(f"[RATE LIMIT] Total token count ({self.total_token_count}) + estimated ({estimated_tokens}) "
                  f"exceeds {TOKEN_THRESHOLD}. Waiting for {wait_time} seconds...")
            time.sleep(wait_time)
            self.total_token_count = 0
            self.minute_start_time = time.time()
            print(f"[DEBUG] Reset token count after wait at {self.minute_start_time}")
        
        self.total_token_count += estimated_tokens
        print(f"[DEBUG] Updated total token count: {self.total_token_count}")

    async def invoke_chain_async(self, chain, input_dict, config, batch_num):
        """Invoke chain asynchronously with error handling and retry logic."""
        max_retries = 3
        estimated_tokens = self.estimate_tokens(input_dict['input'])
        for attempt in range(max_retries):
            try:
                resp = await chain.ainvoke(input_dict, config)
                if hasattr(resp, 'response_headers'):
                    self.last_response_headers = {
                        'x-ratelimit-tokens-remaining': int(resp.response_headers.get('x-ratelimit-tokens-remaining', TOKEN_LIMIT_PER_MINUTE)),
                        'x-ratelimit-tokens-used': int(resp.response_headers.get('x-ratelimit-tokens-used', 0)),
                        'x-ratelimit-reset-tokens': float(resp.response_headers.get('x-ratelimit-reset-tokens', 62))
                    }
                    actual_used = self.last_response_headers.get('x-ratelimit-tokens-used', estimated_tokens)
                    self.total_token_count = max(self.total_token_count, actual_used)
                    print(f"[DEBUG] Batch {batch_num} - Updated token count with actual usage: {self.total_token_count}")
                return resp
            except anthropic.RateLimitError as e:
                if attempt == max_retries - 1:
                    print(f"[ERROR] Batch {batch_num} - Failed after {max_retries} attempts: {e}")
                    raise
                reset_time = float(self.last_response_headers.get('x-ratelimit-reset-tokens', 62))
                print(f"[RATE LIMIT] Batch {batch_num} - Rate limit hit. Waiting {reset_time}s before retry {attempt + 1}/{max_retries}")
                time.sleep(reset_time)
                self.total_token_count = 0
                self.minute_start_time = time.time()
                print(f"[DEBUG] Reset token count after rate limit wait at {self.minute_start_time}")
            except Exception as e:
                print(f"[ERROR] Batch {batch_num} - Failed to invoke chain: {e}")
                raise
        return None

    async def process_batch(self, chain, batch, session_id, history_store, cache, context_cache, variant_map, batch_num):
        """Process a single batch and return corrected paragraphs."""
        try:
            text_segments = []
            segment_counts = []
            placeholder_lists = []
            batch_elements = []
            corrected_paras = []
            for para in batch:
                para_elem, text, f_refs, spec, skip = para
                if skip:
                    print(f"[DEBUG] Batch {batch_num} - Skipped paragraph (TOC/title/heading/table/image): '{text[:30]}...'")
                    corrected_paras.append((para_elem, text, f_refs, spec, True))
                    continue
                batch_elements.append(para)
                parts = split_paragraph(text)
                para_segments = [part[1] for part in parts if part[0] == 'text' and not part[4]]
                para_placeholders = [part[1] for part in parts if part[0] == 'footnote']
                print(f"[DEBUG] Batch {batch_num} - Original paragraph: '{text}'")
                print(f"[DEBUG] Batch {batch_num} - Text segments: {para_segments}")
                print(f"[DEBUG] Batch {batch_num} - Footnotes: {para_placeholders}")
                text_segments.extend(para_segments)
                segment_counts.append(len(para_segments))
                placeholder_lists.append(para_placeholders)
            
            if not text_segments:
                return corrected_paras
            
            segments_to_correct = "\n---\n".join(text_segments)
            
            prev_content, _ = get_all_previous_content(context_cache, batch_num)
            state_json = json.dumps(self.global_acronym_state, sort_keys=True)
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
                "For subsequent occurrences, strictly use the abbreviation alone (e.g., 'NR' instead of 'Northeast Region' or 'Northeast Region (NR)'). "
                "For terms in the variant map, use the abbreviated form (e.g., 'NER' for 'Northeast Region', 'AEP' for 'Act East Policy') if the abbreviation is in the CURRENT ACRONYM STATE.\n\n" +
                segments_to_correct
            )
            
            history = history_store.get(session_id, EnhancedChatMessageHistory())
            history_tokens = sum(int(len(msg.content) * TOKENS_PER_CHAR) for msg in history.messages)
            system_prompt = (
                "You are a precision-focused assistant for abbreviation expansion according to the ISAS Style Guide. "
                "Your task is to expand abbreviations on their first occurrence and use the abbreviated form "
                "thereafter. You MUST preserve footnote placeholders (e.g., §FOOTNOTE:1§) exactly as they appear in the input text. "
                "Do not alter or remove them."
            )
            total_prompt_tokens = (
                self.estimate_tokens(system_prompt) +
                self.estimate_tokens(ABBERI) +
                self.estimate_tokens(segments_to_correct) +
                self.estimate_tokens(full_context) +
                history_tokens
            )
            print(f"[DEBUG] Batch {batch_num} - Estimated prompt tokens: {total_prompt_tokens}")

            if history_tokens > HISTORY_TOKEN_LIMIT:
                print(f"[INFO] Batch {batch_num} - Trimming history as tokens ({history_tokens}) exceed limit ({HISTORY_TOKEN_LIMIT})")
                history.trim_history(HISTORY_TOKEN_LIMIT)
                history_tokens = sum(int(len(msg.content) * TOKENS_PER_CHAR) for msg in history.messages)
                total_prompt_tokens = (
                    self.estimate_tokens(system_prompt) +
                    self.estimate_tokens(ABBERI) +
                    self.estimate_tokens(segments_to_correct) +
                    self.estimate_tokens(full_context) +
                    history_tokens
                )
                print(f"[DEBUG] Batch {batch_num} - Trimmed history, new prompt tokens: {total_prompt_tokens}")

            if total_prompt_tokens > MAX_PROMPT_TOKENS:
                print(f"[WARNING] Batch {batch_num} - Prompt too large ({total_prompt_tokens} tokens). Splitting batch.")
                mid_point = len(batch_elements) // 2 or 1
                sub_batch_1 = batch[:mid_point]
                sub_batch_2 = batch[mid_point:]
                corrected_paras.extend(
                    await self.process_batch(
                        chain, 
                        sub_batch_1, 
                        session_id, 
                        history_store, 
                        cache, 
                        context_cache, 
                        variant_map, 
                        f"{batch_num}.1"
                    )
                )
                corrected_paras.extend(
                    await self.process_batch(
                        chain, 
                        sub_batch_2, 
                        session_id, 
                        history_store, 
                        cache, 
                        context_cache, 
                        variant_map, 
                        f"{batch_num}.2"
                    )
                )
                return corrected_paras

            key = get_cache_key(prompt)
            if key in cache:
                out = cache[key]
                print(f"[INFO] Batch {batch_num} - Retrieved response from cache")
            else:
                self.manage_rate_limit(prompt)
                resp = await self.invoke_chain_async(
                    chain,
                    {'input': prompt},
                    config={'configurable': {'session_id': session_id}},
                    batch_num=batch_num
                )
                out = resp.content.strip()
                out = re.sub(r'^---\n?', '', out)
                cache[key] = out
                save_cache(cache, CACHE_FILE)
                print(f"[INFO] Batch {batch_num} - Saved cache to {CACHE_FILE} with {len(cache)} entries")

            corrected_segments = [s.strip() for s in out.split("\n---\n") if s.strip()]
            print(f"[DEBUG] Batch {batch_num} - Corrected segments from LLM: {corrected_segments}")

            if len(corrected_segments) != len(text_segments):
                print(f"[WARNING] Batch {batch_num} - Expected {len(text_segments)} corrected segments, got {len(corrected_segments)}")
                if len(corrected_segments) > len(text_segments):
                    adjusted_segments = []
                    seg_idx = 0
                    for count in segment_counts:
                        para_segs = corrected_segments[seg_idx:seg_idx+count]
                        if len(para_segs) > count:
                            merged = ' '.join(para_segs)
                            adjusted_segments.append(merged)
                        else:
                            adjusted_segments.extend(para_segs)
                        seg_idx += count
                    corrected_segments = adjusted_segments[:len(text_segments)]
                else:
                    corrected_segments.extend(text_segments[len(corrected_segments):])

            seg_idx = 0
            corrected_texts = []
            for i, (para_elem, original_text, f_refs, spec, _) in enumerate(batch_elements):
                num_segments = segment_counts[i]
                para_placeholders = placeholder_lists[i]
                corrected_text = ""
                text_seg_idx = 0
                for part in split_paragraph(original_text):
                    if part[0] == 'text' and not part[4]:
                        if text_seg_idx < num_segments and seg_idx + text_seg_idx < len(corrected_segments):
                            corrected_text += corrected_segments[seg_idx + text_seg_idx]
                            text_seg_idx += 1
                        else:
                            print(f"[WARNING] Batch {batch_num} - Paragraph {i} - Missing corrected segment {text_seg_idx}, using original: '{part[1]}'")
                            corrected_text += part[1]
                        if part[3]:
                            corrected_text += " "
                    elif part[0] == 'footnote':
                        if part[2]:
                            corrected_text += " "
                        corrected_text += part[1]
                        if part[3]:
                            corrected_text += " "
                print(f"[DEBUG] Batch {batch_num} - Reassembled corrected paragraph {i}: '{corrected_text}'")
                corrected_text = re.sub(r"(§FOOTNOTE:\d+§)\1+", r"\1", corrected_text)
                corrected_texts.append(corrected_text)
                corrected_paras.append((para_elem, corrected_text, f_refs, spec, False))
                seg_idx += num_segments

            reassembled_text = "\n\n".join(corrected_texts)
            new_abbreviations = parse_abbreviations(reassembled_text)
            for abbr in new_abbreviations:
                self.global_acronym_state[abbr] = True
            state_json = json.dumps(self.global_acronym_state, sort_keys=True)
            cache_content = f"ACRONYM_STATE:{state_json}\n\n{reassembled_text}"
            update_context_cache(session_id, batch_num, cache_content)
            print(f"[DEBUG] Batch {batch_num} - Updated acronym state: {self.global_acronym_state}")

            return corrected_paras
        except Exception as e:
            print(f"[ERROR] Batch {batch_num} - Failed to process batch: {e}")
            raise

    def normalize_variants(self, text: str, variant_map: dict) -> str:
        """Normalize text by replacing all variants with their abbreviated form if in global_acronym_state, else canonical key, respecting quotes and acronym state."""
        normalized_text = text
        quote_pat = re.compile(r'(["\'].*?["\'])')
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

        result = []
        for seg_type, seg_text in segments:
            if seg_type == 'quote':
                result.append(seg_text)
                continue
            temp_text = seg_text
            for key, variants in sorted(variant_map.items(), key=lambda x: len(x[0]), reverse=True):
                # Check if the key is an acronym in global_acronym_state
                if key in self.global_acronym_state:
                    target = key  # Use the abbreviation (e.g., 'NER', 'AEP')
                else:
                    target = key  # Use the canonical key (e.g., 'Northeast Region')
                for variant in sorted(variants, key=len, reverse=True):
                    pattern = r'\b' + re.escape(variant) + r'\b'
                    if not FN_PAT.search(variant):
                        temp_text = re.sub(pattern, target, temp_text)
            result.append(temp_text)

        normalized_text = ''.join(result)
        print(f"[DEBUG] Normalized text preview: {normalized_text[:500]}...")
        return normalized_text

    async def process(self, input_path: str, output_path: str, variant_map_path: str = "variant_map.json"):
        try:
            llm_cache_file = Path("llm_response_cache.json")
            if llm_cache_file.exists():
                llm_cache_file.unlink()
                print(f"[INFO] Deleted existing XML to {llm_cache_file}")
            for context_cache_file in Path.cwd().glob("abbrev_context_cache_*.json"):
                context_cache_file.unlink()
                print(f"[INFO] Deleted existing file {context_cache_file}")

            if not Path(variant_map_path).is_file():
                raise FileNotFoundError(f"Variant map JSON not found: {variant_map_path}")
            with open(variant_map_path, encoding="utf-8") as f:
                variant_map = json.load(f)

            tmp = Path('_extract')
            tmp.mkdir(exist_ok=True)
            extract_docx(Path(input_path), tmp)
            doc_xml = tmp / 'word' / 'document.xml'
            tree, paras = extract_paragraphs(doc_xml)

            normalized_paras = []
            for para in paras:
                para_elem, text, f_refs, spec, skip = para
                normalized_text = text if skip else self.normalize_variants(text, variant_map)
                normalized_paras.append((para_elem, normalized_text, f_refs, spec, skip))

            chain, history_store = convo_chain(self.llm)
            session_id = f'doc_{Path(input_path).stem}'

            ctx_paras = [p[1] for p in normalized_paras if not p[4]][:CTX_P]
            ctx = '\n\n'.join(ctx_paras)
            self.manage_rate_limit(ctx)
            await self.invoke_chain_async(
                chain,
                {'input': f'Context for abbreviation expansion:\n{ctx}'},
                config={'configurable': {'session_id': session_id}},
                batch_num=0
            )

            cache = load_cache(CACHE_FILE)
            context_cache = {}
            corrected: List[Para] = []

            batches = [normalized_paras[i:i+BATCH_SIZE] for i in range(0, len(normalized_paras), BATCH_SIZE)]
            print(f"[DEBUG] Processing {len(batches)} batches sequentially")
            for i, batch in enumerate(batches):
                batch_num = i + 1
                corrected.extend(
                    await self.process_batch(
                        chain, 
                        batch,
                        session_id, 
                        history_store, 
                        cache, 
                        context_cache, 
                        variant_map, 
                        batch_num
                    )
                )

            start_time = time.time()
            update_document(tree, doc_xml, corrected)
            update_time = time.time() - start_time
            print(f"[TIMING] Time to update document XML: {update_time:.4f} seconds")

            start_time = time.time()
            rezip_docx(tmp, Path(output_path))
            rezip_time = time.time() - start_time
            print(f"[TIMING] Time to rezip DOCX: {rezip_time:.4f} seconds")

            total_write_time = update_time + rezip_time
            print(f"[TIMING] Total time to write back corrected file: {total_write_time:.4f} seconds")

            shutil.rmtree(tmp)
        except Exception as e:
            print(f"[ERROR] Failed to process document: {e}")
            raise

async def main():
    load_dotenv()
    input_path = "SS.docx"
    output_path = "abbrev_expandedPart123.docx"
    llm = ChatAnthropic(
        model="claude-3-5-haiku-20241022",
        temperature=0.0,
        api_key=os.getenv("ANTHROPIC_API_KEY")
    )
    expander = AbbreviationExpander(llm)
    await expander.process(input_path, output_path)
    print(f"✅ Abbreviation expansion completed successfully. Output saved to: {output_path}")

if __name__ == "__main__":
    asyncio.run(main())