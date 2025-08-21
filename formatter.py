
import os
import zipfile
import shutil
import re
import copy
from lxml import etree as ET
import logging
import argparse

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# XML namespaces
NS = {
    'w':   "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    'xml': "http://www.w3.org/XML/1998/namespace",
}

# Placeholders
FN_PLACEHOLDER    = "§FOOTNOTE:{}§"
SUPER_PLACEHOLDER = r"§SUPER_RUN:(.*?)§"

# Regex to tokenize footnotes, supers, non-text runs, and ***/**/* spans
TOKEN_RE = re.compile(
    r"(§FOOTNOTE:(\d+)§)"       # 1=footnote placeholder, 2=ID
    + r"|(§SUPER_RUN:(\d+)§)"   # 3=superscript placeholder, 4=index
    + r"|(§NON_TEXT_RUN:(\d+)§)"# 5=non-text run placeholder, 6=index
    + r"|\*\*\*(.+?)\*\*\*"     # 7=bold+italic text
    + r"|\*\*(.+?)\*\*"         # 8=bold text
    + r"|\*(.+?)\*"             # 9=italic text
, re.DOTALL)

# This class converts the * added to text back to italic, bold, etc.
class DocxBackToBoldItalic:
    def __init__(self, tmp_dir="tmp_docx_transformer"):
        self.tmp_dir = tmp_dir
        self.default_font = "Calibri"
        self.default_size = "24"  # Size 12 in half-points (12 * 2 = 24)
        logger.debug(f"Initialized DocxTransformer with tmp_dir: {tmp_dir}, font: {self.default_font}, size: {self.default_size}")

    def transform(self, input_path: str, output_path: str):
        """Convert markers (*, **, ***) to Word formatting while preserving footnotes."""
        logger.debug(f"Starting transformation of ** and * to bold and italic words: {input_path} -> {output_path}")
        self._prepare_workspace()
        self.src = input_path
        self.dst = output_path
        self._unzip()
        self._rewrite_document()
        self._rezip()
        shutil.rmtree(self.tmp_dir)
        logger.info(f"✓ Written styled DOCX to {self.dst}")

    def _prepare_workspace(self):
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir)
        os.makedirs(self.tmp_dir)

    def _unzip(self):
        with zipfile.ZipFile(self.src, 'r') as zf:
            zf.extractall(self.tmp_dir)
        self.doc_xml = os.path.join(self.tmp_dir, 'word', 'document.xml')
        if not os.path.exists(self.doc_xml):
            raise FileNotFoundError("Missing document.xml inside DOCX")

    def _rewrite_document(self):
        tree = ET.parse(self.doc_xml)
        root = tree.getroot()
        for p in root.findall('.//w:p', NS):
            if self._is_toc_paragraph(p):
                continue
            flat, footnotes, supers, non_text_runs = self._flatten_paragraph(p)
            logger.debug(f"Flattened paragraph: {flat[:50]}...")
            
            # Check if the entire paragraph is surrounded by **
            stripped_text = flat.strip()
            if stripped_text.startswith("**") and stripped_text.endswith("**") and len(stripped_text) >= 4:
                content = stripped_text[2:-2].strip()
                logger.debug(f"Converting entire paragraph to bold: '{content}'")
                
                # Preserve paragraph properties
                pPr = p.find('w:pPr', NS)
                pPr_copy = copy.deepcopy(pPr) if pPr is not None else None
                
                # Clear all runs
                for child in list(p):
                    if child.tag != f"{{{NS['w']}}}pPr":
                        p.remove(child)
                
                # Restore pPr if it was removed
                if pPr_copy is not None and p.find('w:pPr', NS) is None:
                    p.insert(0, pPr_copy)
                
                # Emit a single bold run for the content
                self._emit_run(p, content, bold=True)
                
                # Check for footnotes or superscripts (unlikely in whole-paragraph bold)
                if footnotes or supers:
                    logger.warning(f"Paragraph with whole bold '**{content}**' contains footnotes or superscripts, which may be ignored")
                
                continue
            
            # Process paragraphs with asterisks within the text
            if not re.search(r'\*\*\*(.*?)\*\*\*|\*\*(.*?)\*\*|\*(.*?)\*', flat, re.DOTALL):
                logger.debug(f"Skipping paragraph, no asterisks found: {flat[:50]}...")
                continue
                
            logger.debug(f"Processing paragraph with asterisks: {flat[:50]}...")
            parent, idx = p.getparent(), p.getparent().index(p)
            backup = copy.deepcopy(p)
            try:
                self._rebuild_paragraph(p, flat, footnotes, supers, non_text_runs)
                if not p.findall('.//w:r', NS):
                    raise RuntimeError("Rebuild emptied paragraph!")
                logger.debug(f"Successfully rebuilt paragraph: {flat[:50]}...")
            except Exception as e:
                logger.warning(f"Restoring original paragraph due to: {e}")
                parent.remove(p)
                parent.insert(idx, backup)
        tree.write(self.doc_xml, encoding='utf-8', xml_declaration=True)

    def _rezip(self):
        with zipfile.ZipFile(self.dst, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(self.tmp_dir):
                for fn in files:
                    full = os.path.join(root, fn)
                    arc = os.path.relpath(full, self.tmp_dir)
                    zf.write(full, arc)

    def _is_heading(self, p):
        st = p.find('.//w:pStyle', NS)
        if st is not None and any(k in st.get(f"{{{NS['w']}}}val","").lower() for k in ("heading","title")):
            return True
        if p.find('.//w:outlineLvl', NS) is not None:
            return True
        return False

    def _flatten_paragraph(self, p):
        flat, footnotes, supers, non_text_runs = "", [], [], []
        for r in p.findall('.//w:r', NS):
            fr = r.find('w:footnoteReference', NS)
            if fr is not None:
                fid = fr.get(f"{{{NS['w']}}}id")
                rPr = copy.deepcopy(r.find('w:rPr', NS))
                footnotes.append((fid, rPr))
                flat += FN_PLACEHOLDER.format(fid)
                logger.debug(f"Found footnote ID {fid}")
                continue
            va = r.find('w:rPr/w:vertAlign', NS)
            if va is not None and va.get(f"{{{NS['w']}}}val") == "superscript":
                raw = ET.tostring(r, encoding='unicode')
                supers.append(raw)
                flat += f"§SUPER_RUN:{len(supers)-1}§"
                logger.debug(f"Found superscript at index {len(supers)-1}")
                continue
            ts = r.findall('w:t', NS)
            if ts:
                text = "".join(t.text or "" for t in ts)
                flat += text
            else:
                raw = ET.tostring(r, encoding='unicode')
                non_text_runs.append(raw)
                flat += f"§NON_TEXT_RUN:{len(non_text_runs)-1}§"
                logger.debug(f"Found non-text run (e.g., image) at index {len(non_text_runs)-1}")
        logger.debug(f"Flattened paragraph: {flat[:50]}...")
        return flat, footnotes, supers, non_text_runs

    def _rebuild_paragraph(self, p, text, footnotes, supers, non_text_runs):
        # Preprocess text to fix **** in bold contexts
        text = re.sub(r'\*\*([^\*]+)\*\*\*\*([^\*]+)\*\*', r'**\1 \2**', text)
        logger.debug(f"Preprocessed text: {text[:50]}...")
        
        # Preserve paragraph properties
        pPr = p.find('w:pPr', NS)
        pPr_copy = copy.deepcopy(pPr) if pPr is not None else None
        
        # Clear all runs
        for child in list(p):
            if child.tag != f"{{{NS['w']}}}pPr":
                p.remove(child)
        
        # Restore pPr if it was removed
        if pPr_copy is not None and p.find('w:pPr', NS) is None:
            p.insert(0, pPr_copy)
        
        fn_idx, last = 0, 0
        for m in TOKEN_RE.finditer(text):
            start, end = m.span()
            if start > last:
                self._emit_run(p, text[last:start])
                logger.debug(f"Emitted unformatted run: {text[last:start][:30]}...")
            if m.group(1):
                fid, rPr = footnotes[fn_idx]
                self._emit_footnote(p, fid, rPr)
                fn_idx += 1
                logger.debug(f"Emitted footnote ID {fid}")
            elif m.group(3):
                idx = int(m.group(4))
                run = ET.fromstring(supers[idx])
                p.append(run)
                logger.debug(f"Emitted superscript run {idx}")
            elif m.group(5):
                idx = int(m.group(6))
                run = ET.fromstring(non_text_runs[idx])
                p.append(run)
                logger.debug(f"Emitted non-text run (e.g., image) {idx}")
            elif m.group(7):
                self._emit_run(p, m.group(7), bold=True, italic=True)
                logger.debug(f"Emitted bold+italic run: {m.group(7)[:30]}...")
            elif m.group(8):
                self._emit_run(p, m.group(8), bold=True)
                logger.debug(f"Emitted bold run: {m.group(8)[:30]}...")
            elif m.group(9):
                self._emit_run(p, m.group(9), italic=True)
                logger.debug(f"Emitted italic run: {m.group(9)[:30]}...")
            last = end
        if last < len(text):
            self._emit_run(p, text[last:])
            logger.debug(f"Emitted remaining text: {text[last:][:30]}...")

    def _emit_run(self, p, txt, bold=False, italic=False):
        if not txt:
            return
        r = ET.SubElement(p, f"{{{NS['w']}}}r")
        rPr = ET.SubElement(r, f"{{{NS['w']}}}rPr")
        # Add font family
        rFonts = ET.SubElement(rPr, f"{{{NS['w']}}}rFonts")
        rFonts.set(f"{{{NS['w']}}}ascii", self.default_font)
        rFonts.set(f"{{{NS['w']}}}hAnsi", self.default_font)
        # Add font size
        sz = ET.SubElement(rPr, f"{{{NS['w']}}}sz")
        sz.set(f"{{{NS['w']}}}val", self.default_size)
        szCs = ET.SubElement(rPr, f"{{{NS['w']}}}szCs")
        szCs.set(f"{{{NS['w']}}}val", self.default_size)
        # Add bold/italic
        if bold:
            ET.SubElement(rPr, f"{{{NS['w']}}}b")
        if italic:
            ET.SubElement(rPr, f"{{{NS['w']}}}i")
        t = ET.SubElement(r, f"{{{NS['w']}}}t")
        t.set(f"{{{NS['w']}}}space", "preserve")
        t.text = txt
        logger.debug(f"Emitted run with bold={bold}, italic={italic}, font={self.default_font}, size={self.default_size}: {txt[:30]}...")

    def _emit_footnote(self, p, fid, rPr):
        r = ET.SubElement(p, f"{{{NS['w']}}}r")
        if rPr is not None:
            r.append(copy.deepcopy(rPr))
        fr = ET.SubElement(r, f"{{{NS['w']}}}footnoteReference")
        fr.set(f"{{{NS['w']}}}id", fid)

    def _is_toc_paragraph(self, p):
        st = p.find('.//w:pStyle', NS)
        if st is not None and "toc" in st.get(f"{{{NS['w']}}}val", "").lower():
            return True
        return False

# This class detects already bold and italic words and changes them to *
class DocxFormatterDetector:
    def __init__(self, tmp_dir="tmp_docx_detector"):
        self.tmp_dir = tmp_dir
        logger.debug(f"Initialized DocxFormatterDetector with tmp_dir: {tmp_dir}")

    def detect_and_convert(self, input_path: str, output_path: str):
        """Detect bold/italic formatting and convert to markers, preserving footnotes."""
        logger.debug(f"Starting detection and conversion of already bold and italic words: {input_path} -> {output_path}")
        self._prepare_workspace()
        self.src = input_path
        self.dst = output_path
        self._unzip()
        self._rewrite_document()
        self._rezip()
        shutil.rmtree(self.tmp_dir)
        logger.info(f"✓ Written modified DOCX to {self.dst}")

    def _prepare_workspace(self):
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir)
        os.makedirs(self.tmp_dir)

    def _unzip(self):
        with zipfile.ZipFile(self.src, 'r') as zf:
            zf.extractall(self.tmp_dir)
        self.doc_xml = os.path.join(self.tmp_dir, 'word', 'document.xml')
        if not os.path.exists(self.doc_xml):
            raise FileNotFoundError("Missing document.xml inside DOCX")

    def _rewrite_document(self):
        tree = ET.parse(self.doc_xml)
        root = tree.getroot()
        for p in root.findall('.//w:p', NS):
            if self._is_toc_paragraph(p):
                continue
            # Check if it's a Markdown-style heading
            flat_text = "".join(t.text or "" for r in p.findall('.//w:r', NS) for t in r.findall('w:t', NS))
            is_markdown_heading = flat_text.strip().startswith('#')
            
            # Load styles.xml to check style-based formatting for headings
            styles_tree = ET.parse(os.path.join(self.tmp_dir, 'word', 'styles.xml'))
            styles_root = styles_tree.getroot()
            
            new_runs = []
            accumulated_text = ""
            accumulated_bold = False
            accumulated_italic = False
            accumulated_trailing = ""

            def emit_accumulated():
                nonlocal accumulated_text, accumulated_bold, accumulated_italic, accumulated_trailing
                if accumulated_text.strip():  # Only emit if there's non-whitespace content
                    content = accumulated_text.rstrip()
                    trailing = accumulated_text[len(content):] + accumulated_trailing
                    if accumulated_bold and accumulated_italic:
                        new_text = f"***{content}***{trailing}"
                    elif accumulated_bold:
                        new_text = f"**{content}**{trailing}"
                    elif accumulated_italic:
                        new_text = f"*{content}*{trailing}"
                    else:
                        new_text = content + trailing
                    new_r = ET.Element(f"{{{NS['w']}}}r")
                    new_rPr = ET.SubElement(new_r, f"{{{NS['w']}}}rPr")
                    # Add font family
                    rFonts = ET.SubElement(new_rPr, f"{{{NS['w']}}}rFonts")
                    rFonts.set(f"{{{NS['w']}}}ascii", "Calibri")
                    rFonts.set(f"{{{NS['w']}}}hAnsi", "Calibri")
                    # Add font size
                    sz = ET.SubElement(new_rPr, f"{{{NS['w']}}}sz")
                    sz.set(f"{{{NS['w']}}}val", "24")
                    szCs = ET.SubElement(new_rPr, f"{{{NS['w']}}}szCs")
                    szCs.set(f"{{{NS['w']}}}val", "24")
                    # Add italic if needed, but skip bold
                    if accumulated_italic:
                        ET.SubElement(new_rPr, f"{{{NS['w']}}}i")
                    new_t = ET.SubElement(new_r, f"{{{NS['w']}}}t")
                    new_t.set(f"{{{NS['xml']}}}space", "preserve")
                    new_t.text = new_text
                    new_runs.append(new_r)
                    logger.debug(f"Emitted accumulated text: {new_text[:30]}...")
                accumulated_text = ""
                accumulated_bold = False
                accumulated_italic = False
                accumulated_trailing = ""

            for r in p.findall('.//w:r', NS):
                # Preserve footnote runs
                if r.find('w:footnoteReference', NS) is not None:
                    emit_accumulated()
                    new_runs.append(copy.deepcopy(r))
                    logger.debug("Preserved footnote run")
                    continue
                # Preserve superscript runs
                if r.find('w:rPr/w:vertAlign[@w:val="superscript"]', NS) is not None:
                    emit_accumulated()
                    new_runs.append(copy.deepcopy(r))
                    logger.debug("Preserved superscript run")
                    continue
                # Preserve non-text runs (e.g., images)
                if len(r.findall('w:t', NS)) == 0:
                    emit_accumulated()
                    new_runs.append(copy.deepcopy(r))
                    logger.debug("Preserved non-text run (e.g., image)")
                    continue
                # Process regular text runs
                rPr = r.find('w:rPr', NS)
                text = "".join(t.text or "" for t in r.findall('w:t', NS))
                if not text and not rPr:  # Skip empty runs with no formatting
                    continue
                
                # Special handling for Markdown-style headings
                if is_markdown_heading and text.startswith('#'):
                    emit_accumulated()
                    prefix_parts = text.split(' ', 1)
                    if len(prefix_parts) > 1:
                        prefix, content = prefix_parts
                        content = content.rstrip()
                        trailing_spaces = text[len(prefix) + 1 + len(content):]
                        bold = rPr is not None and rPr.find('w:b', NS) is not None
                        italic = rPr is not None and rPr.find('w:i', NS) is not None
                        if bold and italic:
                            new_text = f"{prefix} ***{content}***{trailing_spaces}"
                        elif bold:
                            new_text = f"{prefix} **{content}**{trailing_spaces}"
                        elif italic:
                            new_text = f"{prefix} *{content}*{trailing_spaces}"
                        else:
                            new_text = text
                    else:
                        new_text = text
                    new_r = ET.Element(f"{{{NS['w']}}}r")
                    new_rPr = ET.SubElement(new_r, f"{{{NS['w']}}}rPr")
                    # Add font family
                    rFonts = ET.SubElement(new_rPr, f"{{{NS['w']}}}rFonts")
                    rFonts.set(f"{{{NS['w']}}}ascii", "Calibri")
                    rFonts.set(f"{{{NS['w']}}}hAnsi", "Calibri")
                    # Add font size
                    sz = ET.SubElement(new_rPr, f"{{{NS['w']}}}sz")
                    sz.set(f"{{{NS['w']}}}val", "24")
                    szCs = ET.SubElement(new_rPr, f"{{{NS['w']}}}szCs")
                    szCs.set(f"{{{NS['w']}}}val", "24")
                    # Add italic if needed, skip bold
                    if italic:
                        ET.SubElement(new_rPr, f"{{{NS['w']}}}i")
                    new_t = ET.SubElement(new_r, f"{{{NS['w']}}}t")
                    new_t.set(f"{{{NS['xml']}}}space", "preserve")
                    new_t.text = new_text
                    new_runs.append(new_r)
                    logger.debug(f"Processed Markdown-style heading run: {new_text[:30]}...")
                    continue
                
                # Normal text processing (including Word headings)
                bold = rPr is not None and rPr.find('w:b', NS) is not None
                italic = rPr is not None and rPr.find('w:i', NS) is not None
                is_heading = self._is_heading(p)
                if is_heading and not bold:
                    # Check style for bold formatting
                    pStyle = p.find('.//w:pStyle', NS)
                    if pStyle is not None:
                        style_id = pStyle.get(f"{{{NS['w']}}}val")
                        style = styles_root.find(f".//w:style[@w:styleId='{style_id}']", NS)
                        if style is not None:
                            style_rPr = style.find('w:rPr', NS)
                            if style_rPr is not None and style_rPr.find('w:b', NS) is not None:
                                bold = True
                                logger.debug(f"Detected bold from style '{style_id}' for heading")
                
                if is_heading:
                    logger.debug(f"Processing heading run: {text[:30]}..., bold={bold}, italic={italic}")
                
                # If formatting matches accumulated, append to it
                if bold == accumulated_bold and italic == accumulated_italic:
                    accumulated_text += text or ""
                else:
                    # Emit accumulated text if any
                    emit_accumulated()
                    # Start new accumulation
                    accumulated_text = text or ""
                    accumulated_bold = bold
                    accumulated_italic = italic
            
            # Emit any remaining accumulated text
            emit_accumulated()
                
            # Replace original runs with new ones
            for child in list(p):
                if child.tag == f"{{{NS['w']}}}r":
                    p.remove(child)
            for new_r in new_runs:
                p.append(new_r)
        tree.write(self.doc_xml, encoding='utf-8', xml_declaration=True)
        
    def _is_heading(self, p):
        st = p.find('.//w:pStyle', NS)
        if st is not None and any(k in st.get(f"{{{NS['w']}}}val","").lower() for k in ("heading","title")):
            return True
        if p.find('.//w:outlineLvl', NS) is not None:
            return True
        return False

    def _rezip(self):
        with zipfile.ZipFile(self.dst, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(self.tmp_dir):
                for fn in files:
                    full = os.path.join(root, fn)
                    arc = os.path.relpath(full, self.tmp_dir)
                    zf.write(full, arc)

    def _is_toc_paragraph(self, p):
        st = p.find('.//w:pStyle', NS)
        if st is not None and "toc" in st.get(f"{{{NS['w']}}}val", "").lower():
            return True
        return False

def main():
    """Run DocxFormatterDetector and/or DocxBackToBoldItalic based on command-line argument."""
    parser = argparse.ArgumentParser(description="Process DOCX file for formatting detection and transformation.")
    parser.add_argument(
        "--operation",
        choices=["detect", "transform", "both"],
        default="both",
        help="Operation to perform: 'detect' for DocxFormatterDetector, 'transform' for DocxBackToBoldItalic, 'both' for sequential execution (default: both)"
    )
    args = parser.parse_args()

    input_path = "South Asia.docx"         # Replace with your input file
    intermediate_path = "docpart.docx"  # Intermediate file with markers
    output_path = "BacktoItalicBold.docx"       # Final output file

    if args.operation in ["detect", "both"]:
        print("Running DocxFormatterDetector...")
        # Step 1: Detect formatting and convert to markers
        detector = DocxFormatterDetector(tmp_dir="tmp_docx_detector")
        detector.detect_and_convert(input_path, intermediate_path)
        print(f"DocxFormatterDetector complete. Output saved to {intermediate_path}")

    if args.operation in ["transform", "both"]:
        print("Running DocxBackToBoldItalic...")
        # Step 2: Convert markers to formatting
        transformer = DocxBackToBoldItalic(tmp_dir="tmp_docx_transformer")
        transformer.transform(intermediate_path, output_path)
        print(f"DocxBackToBoldItalic complete. Output saved to {output_path}")

if __name__ == "__main__":
    main()
