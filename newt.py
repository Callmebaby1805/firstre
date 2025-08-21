
from __future__ import annotations

import os
import time
import zipfile
import shutil
import argparse
import re
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional
from copy import deepcopy
import asyncio

from lxml import etree
from dotenv import load_dotenv

# Optional UK spelling helper (breame)
try:
    from breame.spelling import (
        get_british_spelling,
        american_spelling_exists,
        british_spelling_exists,
    )
    _BREAME_OK = True
except Exception:
    _BREAME_OK = False

# LLM
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_community.chat_message_histories import ChatMessageHistory

# External prompt + cache utils
from prompt import MAJOR_PROMPT  # expects your existing prompt.py
from cache_utils import (
    load_cache, save_cache, get_cache_key,
)

import anthropic

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------
NS = {
    'w': "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    'xml': "http://www.w3.org/XML/1998/namespace",
}

FN_PLACEHOLDER = "§FOOTNOTE:{}§"
FN_PAT = re.compile(r"§FOOTNOTE:(\d+)§")
CTX_P = 10  # number of initial paragraphs to prime context
CACHE_FILE = Path("llm_response_cache.json")
BATCH_SIZE = 5

# Rate limiting constants
TOKEN_LIMIT_PER_MINUTE = 40000
TOKENS_PER_CHAR = 0.35  # ~4 chars per token
TOKEN_THRESHOLD = 39000  # Wait when token count reaches 39,000

Para = Tuple[etree._Element, str, List[Tuple[int, str]], List[Dict[str, Any]], bool]


# --------------------------------------------------------------------------------------
# DOCX helpers
# --------------------------------------------------------------------------------------
def extract_docx(src: Path, dst: Path):
    with zipfile.ZipFile(src, 'r') as zf:
        zf.extractall(dst)

def rezip_docx(src_dir: Path, out: Path):
    if out.exists():
        out.unlink()
    shutil.make_archive(out.with_suffix(''), 'zip', src_dir)
    (out.with_suffix('').with_suffix('.zip')).rename(out)

def set_font_to_calibri(rPr: etree._Element):
    rFonts = rPr.find('.//w:rFonts', NS)
    if rFonts is None:
        rFonts = etree.SubElement(rPr, f"{{{NS['w']}}}rFonts")
    rFonts.set(f"{{{NS['w']}}}ascii", "Calibri")
    rFonts.set(f"{{{NS['w']}}}hAnsi", "Calibri")


# --------------------------------------------------------------------------------------
# Paragraph classification
# --------------------------------------------------------------------------------------
def is_heading_or_title(p_elem: etree._Element, is_first_non_empty: bool) -> bool:
    style_elem = p_elem.find('.//w:pStyle', NS)
    if style_elem is not None:
        val = style_elem.get(f"{{{NS['w']}}}val", '').lower()
        if 'heading' in val or 'title' in val:
            return True
    if is_first_non_empty:
        return True
    if p_elem.find('.//w:b', NS) is not None:
        text = ''.join(t.text or '' for t in p_elem.findall('.//w:t', NS))
        if len(text.split()) <= 7:
            return True
    return False

def is_table_paragraph(p_elem: etree._Element) -> bool:
    current = p_elem
    while current is not None:
        if current.tag == f"{{{NS['w']}}}tbl":
            return True
        current = current.getparent()
    return False

def is_image_paragraph(p_elem: etree._Element) -> bool:
    return p_elem.find('.//w:drawing', NS) is not None or p_elem.find('.//w:pict', NS) is not None

def is_toc_paragraph(p_elem: etree._Element, text: str, is_after_contents: bool) -> bool:
    style_elem = p_elem.find('.//w:pStyle', NS)
    if style_elem is not None and 'toc' in style_elem.get(f"{{{NS['w']}}}val", '').lower():
        return True
    text_lower = text.strip().lower()
    if text_lower == 'contents':
        return True
    if is_after_contents:
        return bool(text.strip())
    return False

def is_section_heading(text: str) -> bool:
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
        runs = p.findall('.//w:r', NS)
        text_chunks: List[Tuple[int, str]] = []
        f_refs: List[Tuple[int, str]] = []
        special_elements: List[Dict[str, Any]] = []
        for idx, r in enumerate(runs):
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
        has_text = bool(combined.strip())
        if has_text and not first_non_empty_found:
            first_non_empty_found = True
        is_toc = is_toc_paragraph(p, combined, is_after_contents)
        if combined.strip().lower() == 'archive':
            is_after_contents = True
        if is_after_contents and is_section_heading(combined):
            is_after_contents = False
        skip_processing = (
            is_toc or
            is_heading_or_title(p, not first_non_empty_found) or
            is_table_paragraph(p) or
            is_image_paragraph(p)
        )
        paras.append((p, combined, f_refs, special_elements, skip_processing))
    return tree, paras


# --------------------------------------------------------------------------------------
# Update paragraph runs (preserve footnotes & styles)
# --------------------------------------------------------------------------------------
def update_paragraph_structure(
    para_elem: etree._Element,
    corrected_text: str,
    special_elements: List[Dict[str, Any]]
) -> None:
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
    for kind, val in segments:
        if kind == 'text':
            if not val:
                continue
            remaining_text = val
            while remaining_text:
                matched_run = None
                if current_run_idx < len(original_runs):
                    matched_run = original_runs[current_run_idx]
                    current_run_idx += 1
                else:
                    matched_run = {'bold': False, 'italic': False, 'formatting': None, 'text': remaining_text}

                text_to_use = matched_run.get('text', remaining_text)

                r = etree.SubElement(para_elem, f"{{{NS['w']}}}r")
                rPr = etree.SubElement(r, f"{{{NS['w']}}}rPr")
                set_font_to_calibri(rPr)
                if matched_run.get('bold'):
                    etree.SubElement(rPr, f"{{{NS['w']}}}b")
                if matched_run.get('italic'):
                    etree.SubElement(rPr, f"{{{NS['w']}}}i")
                if matched_run.get('formatting') is not None:
                    for child in matched_run['formatting']:
                        if child.tag not in {f"{{{NS['w']}}}b", f"{{{NS['w']}}}i", f"{{{NS['w']}}}rFonts"}:
                            rPr.append(deepcopy(child))
                t = etree.SubElement(r, f"{{{NS['w']}}}t", {f"{{{NS['xml']}}}space": "preserve"})
                t.text = text_to_use
                remaining_text = remaining_text[len(text_to_use):]
        else:
            fid = val
            r = etree.SubElement(para_elem, f"{{{NS['w']}}}r")
            fmt = footnote_fmt.get(fid)
            if fmt is not None:
                r.append(deepcopy(fmt))
            etree.SubElement(r, f"{{{NS['w']}}}footnoteReference", {f"{{{NS['w']}}}id": fid})

def update_document(tree: etree._ElementTree, doc_xml: Path, new_paras: List[Para]) -> None:
    for para_elem, txt, f_refs, spec, skip in new_paras:
        if not skip:
            update_paragraph_structure(para_elem, txt, spec)
    tree.write(str(doc_xml), encoding='utf-8', xml_declaration=True, pretty_print=True)


# --------------------------------------------------------------------------------------
# Chat History / Chain
# --------------------------------------------------------------------------------------
class EnhancedChatMessageHistory(ChatMessageHistory):
    def add_message(self, message):
        super().add_message(message)
        return message
    def get_context_summary(self) -> str:
        msgs = self.messages[-3:]
        return '\n'.join(m.content for m in msgs)

def convo_chain(llm: ChatAnthropic):
    system_prompt = (
        "You are a helpful assistant correcting documents according to the ISAS Style Guide. "
        "The input segments have already been normalised for numbers (1–9 spelled out), percentages ('per cent'), "
        "temperatures ('degrees Celsius/Fahrenheit'), and general UK spelling where applicable. "
        "Do NOT alter acronyms. "
        "When correcting the text, you MUST preserve the footnote placeholders (e.g., §FOOTNOTE:1§) exactly as they appear. "
        "Your focus is limited to foreign (non-English) words/phrases that need italicising and adding concise English glosses "
        "on first use per ISAS rules. Do not change quotations, tables, or headers/footers."
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


# --------------------------------------------------------------------------------------
# Footnote-aware segmenter
# --------------------------------------------------------------------------------------
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


# --------------------------------------------------------------------------------------
# Deterministic rules (numbers, temps, percentages, pluralise units, UK spelling)
# --------------------------------------------------------------------------------------
def _normalize_thin_spaces(s: str) -> str:
    return s.replace("\u2009", " ").replace("\u00a0", " ")

def _degree_word(n: float) -> str:
    return "degree" if abs(n) == 1 else "degrees"

def normalize_temperatures(text: str) -> str:
    def repl_symbol(m: re.Match) -> str:
        raw = m.group('rawnum')
        num = float(m.group("num"))
        unit = m.group("unit").upper()
        unit_full = "Celsius" if unit == "C" else "Fahrenheit"
        return f"{raw} {_degree_word(num)} {unit_full}"
    def repl_wordy(m: re.Match) -> str:
        raw = m.group('rawnum')
        num = float(m.group("num"))
        unit = m.group("unit").upper()
        unit_full = "Celsius" if unit == "C" else "Fahrenheit"
        return f"{raw} {_degree_word(num)} {unit_full}"
    text = re.sub(r"(?P<rawnum>-?\d+(?:\.\d+)?)\s*°\s*(?P<unit>[cCfF])", repl_symbol, text)
    text = re.sub(r"(?P<rawnum>-?\d+(?:\.\d+)?)\s*(?:deg(?:ree)?s?)\s*(?P<unit>[cCfF])\b", repl_wordy, text, flags=re.IGNORECASE)
    return text

_NUM_WORDS = {
    0:"zero",1:"one",2:"two",3:"three",4:"four",5:"five",
    6:"six",7:"seven",8:"eight",9:"nine",10:"ten",11:"eleven",
    12:"twelve",13:"thirteen",14:"fourteen",15:"fifteen",
    16:"sixteen",17:"seventeen",18:"eighteen",19:"nineteen"
}
_TENS = {20:"twenty",30:"thirty",40:"forty",50:"fifty",60:"sixty",70:"seventy",80:"eighty",90:"ninety"}

def _int_to_words(n: int) -> str:
    if n < 0 or n > 999: return str(n)
    if n < 20: return _NUM_WORDS[n]
    if n < 100:
        t, r = divmod(n, 10)
        return _TENS[t*10] + (f"-{_NUM_WORDS[r]}" if r else "")
    h, r = divmod(n, 100)
    if r == 0: return f"{_NUM_WORDS[h]} hundred"
    if r < 20: return f"{_NUM_WORDS[h]} hundred and {_NUM_WORDS[r]}"
    t, o = divmod(r, 10)
    tail = _TENS[t*10] + (f"-{_NUM_WORDS[o]}" if o else "")
    return f"{_NUM_WORDS[h]} hundred and {tail}"

def spell_out_single_digits(text: str) -> str:
    YEAR_RE = r"(?:(?:19|20)\d{2})"
    pattern = re.compile(
        rf"""
        (?<![\w-])
        (?!{YEAR_RE}\b)
        ([0-9])
        (?!\d)
        (?!st\b|nd\b|rd\b|th\b)
        (?!\s*[\u2013\u2014\-]\s*\d)
        (?!\.\d)
        (?![A-Za-z])
        """, re.VERBOSE
    )
    return pattern.sub(lambda m: _NUM_WORDS[int(m.group())], text)

def _fix_sentence_start(sentence: str) -> str:
    s = sentence.lstrip()
    leading_ws = sentence[:len(sentence)-len(s)]
    m = re.match(r"^(\d{1,3})(\b|$)", s)
    if m:
        s = _int_to_words(int(m.group(1))) + s[m.end():]
    return leading_ws + s

def no_sentence_starts_with_digit(text: str) -> str:
    parts = re.split(r"(\s*[.!?]\s+)", text)
    if len(parts) == 1:
        return _fix_sentence_start(parts[0])
    fixed = []
    for i in range(0, len(parts), 2):
        sent = parts[i]
        delim = parts[i+1] if i+1 < len(parts) else ""
        fixed.append(_fix_sentence_start(sent) + delim)
    return "".join(fixed)

def pluralize_units_for_decimals(text: str) -> str:
    def repl(m):
        num = float(m.group("num"))
        unit = m.group("unit")
        if num > 1.0 and not unit.lower().endswith("s"):
            return f"{m.group('prefix')}{m.group('num')} {unit}s"
        return m.group(0)
    return re.sub(
        r"(?P<prefix>\b)(?P<num>\d+\.\d+)\s+(?P<unit>[A-Za-z]+)\b",
        repl,
        text
    )

# Percentages — outside quotes
_WORD_NUMS_0_19 = {
    "zero":0,"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,
    "ten":10,"eleven":11,"twelve":12,"thirteen":13,"fourteen":14,"fifteen":15,"sixteen":16,
    "seventeen":17,"eighteen":18,"nineteen":19
}
_WORD_TENS = {"twenty":20,"thirty":30,"forty":40,"fifty":50,"sixty":60,"seventy":70,"eighty":80,"ninety":90}

def _words_to_int_0_99(s: str) -> Optional[int]:
    s = s.strip().lower()
    if s in _WORD_NUMS_0_19: return _WORD_NUMS_0_19[s]
    if s in _WORD_TENS: return _WORD_TENS[s]
    if "-" in s:
        a, b = s.split("-", 1)
        if a in _WORD_TENS and b in _WORD_NUMS_0_19:
            return _WORD_TENS[a] + _WORD_NUMS_0_19[b]
    return None

_QUOTE_SPLIT_RE = re.compile(r'(".*?"|“.*?”|‘.*?’|\'.*?\')', flags=re.DOTALL)
def _apply_outside_quotes(text: str, transform) -> str:
    parts = _QUOTE_SPLIT_RE.split(text)
    for i in range(len(parts)):
        if i % 2 == 0:
            parts[i] = transform(parts[i])
    return "".join(parts)

def normalize_percentages(text: str) -> str:
    def _tx(seg: str) -> str:
        if not seg: return seg
        seg = re.sub(
            r'(?P<a>\d+(?:\.\d+)?)\s*[-–]\s*(?P<b>\d+(?:\.\d+)?)\s*(%|percent\b|per\s*cent\b)',
            lambda m: f"{m.group('a')}–{m.group('b')} per cent",
            seg, flags=re.IGNORECASE
        )
        seg = re.sub(r'(?P<n>\d+(?:\.\d+)?)\s*%', lambda m: f"{m.group('n')} per cent", seg)
        seg = re.sub(r'(?P<n>\d+(?:\.\d+)?)\s*percent\b', lambda m: f"{m.group('n')} per cent", seg, flags=re.IGNORECASE)
        def repl_wordnum(m):
            val = _words_to_int_0_99(m.group('w'))
            return f"{val} per cent" if val is not None else m.group(0)
        seg = re.sub(r'\b(?P<w>[A-Za-z]+(?:-[A-Za-z]+)*)\s+(?:percent\b|per\s*cent\b)', repl_wordnum, seg, flags=re.IGNORECASE)
        seg = re.sub(r'\bpercent\b', 'per cent', seg, flags=re.IGNORECASE)
        return seg
    return _apply_outside_quotes(text, _tx)

# UK spelling with exceptions + “don’t touch acronyms”
ORG_EXCEPTIONS = {
    "World Health Organization",
    "World Trade Organization",
    "International Civil Aviation Organization",
}
ORG_EXCEPTIONS_LOWER = {s.lower(): s for s in ORG_EXCEPTIONS}
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")

def _is_acronym(token: str) -> bool:
    if len(token) <= 1: return False
    caps = sum(1 for c in token if c.isupper())
    return (caps / len(token)) >= 0.8

def _is_exception_window(text: str, start: int, end: int) -> bool:
    window = text[max(0, start-60):min(len(text), end+60)].lower()
    for exc in ORG_EXCEPTIONS_LOWER:
        if exc in window:
            return True
    return False

def _to_uk_spelling_token(token: str) -> str:
    if _is_acronym(token) or not _BREAME_OK:
        return token
    base = token
    caps = base.isupper()
    title = base.istitle()
    if _BREAME_OK and 'british_spelling_exists' in globals() and british_spelling_exists(base.lower()):
        return base
    if _BREAME_OK and 'american_spelling_exists' in globals() and american_spelling_exists(base.lower()):
        uk = get_british_spelling(base.lower()) or base
        if caps: uk = uk.upper()
        elif title: uk = uk.capitalize()
        return uk
    return base

def britishise_text_with_exceptions(text: str) -> str:
    if not _BREAME_OK:
        return text
    out = []
    idx = 0
    for m in _WORD_RE.finditer(text):
        start, end = m.span()
        out.append(text[idx:start])
        token = m.group(0)
        if _is_exception_window(text, start, end):
            out.append(token)
        else:
            out.append(_to_uk_spelling_token(token))
        idx = end
    out.append(text[idx:])
    result = "".join(out)
    # Reinstate canonical casing for exceptions
    low = result.lower()
    for exc_low, exc_canon in ORG_EXCEPTIONS_LOWER.items():
        pos = 0
        while True:
            i = low.find(exc_low, pos)
            if i == -1:
                break
            result = result[:i] + exc_canon + result[i+len(exc_canon):]
            low = result.lower()
            pos = i + len(exc_canon)
    return result

def preprocess_text_segment(txt: str) -> str:
    """Deterministic pre-pass: thin spaces → space, temps, single digits, percentages,
    sentence starts, pluralise decimal units, UK spelling (if breame available)."""
    txt = _normalize_thin_spaces(txt)
    txt = normalize_temperatures(txt)
    txt = spell_out_single_digits(txt)
    txt = normalize_percentages(txt)
    txt = no_sentence_starts_with_digit(txt)
    txt = pluralize_units_for_decimals(txt)
    txt = britishise_text_with_exceptions(txt)  # acronyms kept as-is
    return txt


# --------------------------------------------------------------------------------------
# Corrector
# --------------------------------------------------------------------------------------
class TextCorrector:
    def __init__(self, llm: ChatAnthropic):
        self.llm = llm
        self.token_count = 0
        self.last_reset_time = time.time()
        self.last_response_headers = {}

    def estimate_tokens(self, text: str) -> int:
        estimated = int(len(text) * TOKENS_PER_CHAR)
        actual = self.last_response_headers.get('x-ratelimit-tokens-used', None)
        if actual is not None:
            print(f"[DEBUG] Estimated tokens: {estimated}, Actual tokens (from headers): {actual}")
        return estimated

    def manage_rate_limit(self, prompt: str):
        current_time = time.time()
        if current_time - self.last_reset_time >= 60:
            self.token_count = 0
            self.last_reset_time = current_time

        estimated_tokens = self.estimate_tokens(prompt)
        remaining_tokens = self.last_response_headers.get('x-ratelimit-tokens-remaining', TOKEN_LIMIT_PER_MINUTE)
        reset_time = float(self.last_response_headers.get('x-ratelimit-reset-tokens', 60))

        if (self.token_count + estimated_tokens >= TOKEN_THRESHOLD or remaining_tokens < estimated_tokens):
            time.sleep(reset_time)
            self.token_count = 0
            self.last_reset_time = time.time()

        self.token_count += estimated_tokens

    async def invoke_chain_async(self, chain, input_dict, config, batch_num):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = await chain.ainvoke(input_dict, config)
                if hasattr(resp, 'response_headers'):
                    self.last_response_headers = {
                        'x-ratelimit-tokens-remaining': int(resp.response_headers.get('x-ratelimit-tokens-remaining', TOKEN_LIMIT_PER_MINUTE)),
                        'x-ratelimit-tokens-used': int(resp.response_headers.get('x-ratelimit-tokens-used', 0)),
                        'x-ratelimit-reset-tokens': float(resp.response_headers.get('x-ratelimit-reset-tokens', 60))
                    }
                return resp
            except anthropic.RateLimitError as e:
                if attempt == max_retries - 1:
                    print(f"[ERROR] Batch {batch_num} - Rate limited after {max_retries} attempts: {e}")
                    raise
                reset_time = float(self.last_response_headers.get('x-ratelimit-reset-tokens', 60))
                time.sleep(reset_time)
                self.token_count = 0
                self.last_reset_time = time.time()
            except Exception as e:
                print(f"[ERROR] Batch {batch_num} - Failed to invoke chain: {e}")
                raise
        return None

    async def process_batch(self, chain, batch, session_id, cache, batch_num):
        try:
            text_segments = []
            segment_counts = []
            placeholder_lists = []
            batch_elements = []
            corrected_paras = []
            for para in batch:
                para_elem, text, f_refs, spec, skip = para
                if skip:
                    corrected_paras.append((para_elem, text, f_refs, spec, True))
                    continue
                batch_elements.append(para)

                # Split by footnotes to get text-only segments
                parts = split_paragraph(text)
                para_segments = [part[1] for part in parts if part[0] == 'text' and not part[4]]
                para_placeholders = [part[1] for part in parts if part[0] == 'footnote']

                # Deterministic pre-pass here (numbers, temps, percentages, UK spelling, etc.)
                para_segments = [preprocess_text_segment(seg) for seg in para_segments]

                text_segments.extend(para_segments)
                segment_counts.append(len(para_segments))
                placeholder_lists.append(para_placeholders)

            if not text_segments:
                return corrected_paras

            segments_to_correct = "\n---\n".join(text_segments)

            prompt = (
                MAJOR_PROMPT +
                f"\nCorrect the following {len(text_segments)} text segments according to the style guide. "
                "Each segment is separated by '---'. Preserve the separators in your response and return exactly "
                f"{len(text_segments)} segments, maintaining the order and content structure. "
                "Do not add extra separators or modify footnote placeholders.\n\n" +
                segments_to_correct
            )

            key = get_cache_key(prompt)
            if key in cache:
                out = cache[key]
            else:
                self.manage_rate_limit(prompt)
                resp = await self.invoke_chain_async(
                    chain,
                    {'input': prompt},
                    config={'configurable': {'session_id': session_id}},
                    batch_num=batch_num
                )
                out = resp.content.strip().strip('-\n')
                cache[key] = out
                save_cache(cache, CACHE_FILE)

            corrected_segments = [s.strip() for s in out.split("\n---\n") if s.strip()]
            if len(corrected_segments) != len(text_segments):
                # Best-effort reconcile
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
            for i, (para_elem, original_text, f_refs, spec, _) in enumerate(batch_elements):
                num_segments = segment_counts[i]
                corrected_text = ""
                text_seg_idx = 0
                for part in split_paragraph(original_text):
                    if part[0] == 'text' and not part[4]:
                        if text_seg_idx < num_segments and seg_idx + text_seg_idx < len(corrected_segments):
                            corrected_text += corrected_segments[seg_idx + text_seg_idx]
                            text_seg_idx += 1
                        else:
                            corrected_text += part[1]
                        if part[3]:
                            corrected_text += " "
                    elif part[0] == 'footnote':
                        if part[1] not in corrected_text:
                            if part[2]:
                                corrected_text += " "
                            corrected_text += part[1]
                            if part[3]:
                                corrected_text += " "
                corrected_text = re.sub(r"(§FOOTNOTE:\d+§)\1+", r"\1", corrected_text)
                corrected_paras.append((para_elem, corrected_text, f_refs, spec, False))
                seg_idx += num_segments

            return corrected_paras
        except Exception as e:
            print(f"[ERROR] Batch {batch_num} - Failed to process batch: {e}")
            raise

    async def process(self, input_path: str, output_path: str, model: str, temperature: float = 0.1):
        try:
            # (Re)load env on each run so keys from .env are respected
            load_dotenv()
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")

            llm = ChatAnthropic(
                api_key=api_key,
                model=model,
                temperature=temperature,
            )

            # reset cache between full runs
            llm_cache_file = Path("llm_response_cache.json")
            if llm_cache_file.exists():
                llm_cache_file.unlink()

            tmp = Path('_extract')
            tmp.mkdir(exist_ok=True)
            extract_docx(Path(input_path), tmp)
            doc_xml = tmp / 'word' / 'document.xml'
            tree, paras = extract_paragraphs(doc_xml)

            non_empty_paras = [p for p in paras if p[1].strip()]
            chain, _ = convo_chain(llm)
            session_id = f'doc_{Path(input_path).stem}'

            # Warm-up with initial context
            ctx = '\n\n'.join(p[1] for p in non_empty_paras[:CTX_P])
            self.manage_rate_limit(ctx)
            await self.invoke_chain_async(
                chain,
                {'input': f'Context for style:\n{ctx}'},
                config={'configurable': {'session_id': session_id}},
                batch_num=0
            )

            cache = load_cache(CACHE_FILE)
            corrected: List[Para] = []

            batches = [non_empty_paras[i:i+BATCH_SIZE] for i in range(0, len(non_empty_paras), BATCH_SIZE)]
            for i, batch in enumerate(batches):
                batch_num = i + 1
                corrected.extend(await self.process_batch(chain, batch, session_id, cache, batch_num))

            update_document(tree, doc_xml, corrected)
            rezip_docx(tmp, Path(output_path))
            shutil.rmtree(tmp)
        except Exception as e:
            print(f"[ERROR] Failed to process document: {e}")
            raise


# --------------------------------------------------------------------------------------
# CLI / Gradio launcher
# --------------------------------------------------------------------------------------
def _build_arg_parser():
    p = argparse.ArgumentParser(description="ISAS DOCX Corrector (body paragraphs, footnote-safe).")
    p.add_argument("--input", "-i", help="Input DOCX path")
    p.add_argument("--output", "-o", help="Output DOCX path")
    p.add_argument("--model", "-m", default="claude-sonnet-4-20250514", help="Anthropic model name")
    p.add_argument("--temperature", "-t", type=float, default=0.1, help="LLM temperature")
    p.add_argument("--gradio", action="store_true", help="Launch the Gradio UI instead of CLI")
    p.add_argument("--host", default="0.0.0.0", help="Gradio host (default binds to instance IP)")
    p.add_argument("--port", type=int, default=7860, help="Gradio port")
    return p

def launch_gradio(default_model: str = "claude-sonnet-4-20250514"):
    import gradio as gr
    import tempfile

    corrector = TextCorrector(llm=None)  # llm is created inside process()

    def process_docx(file, model, temperature):
        if file is None:
            raise gr.Error("Please upload a .docx file.")
        input_path = Path(file.name)
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / f"{input_path.stem}_corrected.docx"
            # Run the pipeline
            asyncio.run(corrector.process(str(input_path), str(out_path), model=model, temperature=temperature))
            # Return a *copy* that persists after tmpdir closes
            final_path = Path.cwd() / f"{input_path.stem}_corrected.docx"
            shutil.copy(str(out_path), str(final_path))
            return str(final_path)

    with gr.Blocks(title="ISAS Document Styler") as demo:
        gr.Markdown("### Upload a .docx file to apply ISAS style corrections. Footnotes are preserved.")
        with gr.Row():
            in_file = gr.File(file_types=[".docx"], label="Upload .docx")
        with gr.Row():
            model_dd = gr.Dropdown(
                choices=[
                    "claude-sonnet-4-20250514",
                    "claude-3-7-sonnet-20250219",
                ],
                value=default_model,
                label="Anthropic model"
            )
            temp_slider = gr.Slider(0.0, 1.0, value=0.1, step=0.05, label="Temperature")
        out_file = gr.File(label="Download corrected file")

        run_btn = gr.Button("Process")
        run_btn.click(process_docx, inputs=[in_file, model_dd, temp_slider], outputs=[out_file])

    demo.launch(server_name=os.getenv("GRADIO_SERVER_NAME", "0.0.0.0"),
                server_port=int(os.getenv("GRADIO_SERVER_PORT", "7860")),
                share=False)

def main():
    load_dotenv()
    args = _build_arg_parser().parse_args()

    if args.gradio:
        launch_gradio(default_model=args.model)
        return

    if not args.input or not args.output:
        print("Error: --input and --output are required in CLI mode. Or run with --gradio to use the UI.")
        return

    corrector = TextCorrector(llm=None)
    asyncio.run(corrector.process(args.input, args.output, model=args.model, temperature=args.temperature))
    print(f"✅ Correction complete. Output saved to: {args.output}")

if __name__ == "__main__":
    main()
