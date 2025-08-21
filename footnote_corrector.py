import os
import sys
import shutil
import zipfile
from lxml import etree as ET
import re
from dotenv import load_dotenv
import datetime
import urllib.parse

from langchain_community.tools.tavily_search.tool import TavilySearchResults
from langchain_anthropic import ChatAnthropic
from langchain.prompts import ChatPromptTemplate
from fCorrectprompt import FOOTNOTE_PROMPT
import logging

# completely mute urllib3 (used by requests)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# mute httpx (used by ChatAnthropic under the hood)
logging.getLogger("httpx").setLevel(logging.WARNING)

class DocxFootnoteProcessor:
    """
    A class for processing footnotes in Word documents (.docx files)
    using AI-powered verification and formatting.
    """

    def __init__(
        self,
        input_path=None,
        output_path=None,
        debug_folder="debug",
        anthropic_model="claude-3-7-sonnet-20250219",
        temperature=0.1,
    ):
        # Load environment variables
        load_dotenv()

        # Validate API keys
        self.tavily_api_key = os.getenv("TAVILY_API_KEY")
        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
        if not self.tavily_api_key:
            raise RuntimeError("Missing TAVILY_API_KEY environment variable")
        if not self.anthropic_api_key:
            raise RuntimeError("Missing ANTHROPIC_API_KEY environment variable")

        # Set document paths
        self.input_path = input_path
        self.output_path = output_path
        self.debug_folder = debug_folder

        # Initialize API clients
        self.search_tool = TavilySearchResults(
            api_key=self.tavily_api_key,
            max_results=4,  # Limit to 3-4 results
            search_depth="advanced",  # Use advanced search depth
            include_raw_content=True,  # Include raw content for better context
            exclude_domains=["wikipedia.org", "quora.com"],  # Exclude Wikipedia and Quora
        )
        self.claude_llm = ChatAnthropic(
            temperature=temperature,
            anthropic_api_key=self.anthropic_api_key,
            model_name=anthropic_model,
        )

        # Create prompt template
        self.prompt = ChatPromptTemplate.from_template(FOOTNOTE_PROMPT)

        # XML namespaces
        self.W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        self.XML = "http://www.w3.org/XML/1998/namespace"
        self.R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        self.ns = {"w": self.W, "xml": self.XML, "r": self.R}

        # Register namespaces
        ET.register_namespace("w", self.W)
        ET.register_namespace("xml", self.XML)
        ET.register_namespace("r", self.R)

        # Temporary directory for docx extraction
        self.tmp_dir = "tmp_docx"

    def set_paths(self, input_path, output_path=None):
        """Set the input and output document paths."""
        self.input_path = input_path
        if output_path:
            self.output_path = output_path
        else:
            base, ext = os.path.splitext(input_path)
            self.output_path = f"{base}_corrected{ext}"

    def extract_docx(self):
        """Extract the DOCX file to a temporary directory"""
        print("⏳ Extracting original DOCX…")
        with zipfile.ZipFile(self.input_path, 'r') as z:
            z.extractall(self.tmp_dir)
        print("✅ Extraction complete")

    def repackage_docx(self):
        """Repackage the modified files into a new DOCX file"""
        print("📦 Repackaging new DOCX…")
        with zipfile.ZipFile(self.output_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(self.tmp_dir):
                for fn in files:
                    full = os.path.join(root, fn)
                    arc = os.path.relpath(full, self.tmp_dir)
                    z.write(full, arc)
        print(f"✅ New DOCX saved: {self.output_path}")

    def dump_xml(self, element, path):
        """Dump XML element to a file for debugging"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tree_str = ET.tostring(
            element, pretty_print=True, xml_declaration=True, encoding="UTF-8"
        )
        with open(path, "wb") as f:
            f.write(tree_str)

    def parse_markdown_segments(self, text):
        """Parse text with markdown-like formatting into segments."""
        segments = []
        patterns = [
            (r'\*(.*?)\*', 'italic'),
            (r'\((https?://[^\s)]+)\)', 'hyperlink'),
            (r'(https?://[^\s]+)', 'hyperlink'),
        ]

        matches = []
        for pattern, fmt_type in patterns:
            for match in re.finditer(pattern, text):
                content = match.group(1)
                if content.strip():
                    matches.append((match.start(), match.end(), content, fmt_type))
        matches.sort(key=lambda x: x[0])

        last_pos = 0
        for start, end, content, fmt_type in matches:
            if start > last_pos:
                plain = text[last_pos:start]
                if plain.strip():
                    segments.append({'text': plain, 'type': 'plain'})
            segments.append({'text': content, 'type': fmt_type})
            last_pos = end

        if last_pos < len(text):
            plain = text[last_pos:]
            if plain.strip():
                segments.append({'text': plain, 'type': 'plain'})

        return segments

    def update_relationships(self, part_name, new_rels):
        """Update or create relationships file for hyperlinks"""
        rels_dir = os.path.join(self.tmp_dir, 'word', '_rels')
        rels_path = os.path.join(rels_dir, f"{os.path.basename(part_name)}.rels")

        os.makedirs(rels_dir, exist_ok=True)
        parser = ET.XMLParser(remove_blank_text=True)
        if os.path.exists(rels_path):
            tree = ET.parse(rels_path, parser)
            root = tree.getroot()
        else:
            root = ET.Element("{http://schemas.openxmlformats.org/package/2006/relationships}Relationships")
            tree = ET.ElementTree(root)

        for rel_id, target in new_rels:
            rel = ET.SubElement(
                root,
                "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship",
            )
            rel.set("Id", rel_id)
            rel.set(
                "Type",
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
            )
            rel.set("Target", target)
            rel.set("TargetMode", "External")

        tree.write(rels_path, pretty_print=True, xml_declaration=True, encoding="UTF-8")
        return True

    def extract_date_from_url(self, url):
        """Extract date from URL patterns like 2024-07-19"""
        date_patterns = [
            r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})',  # YYYY-MM-DD or YYYY/MM/DD
            r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})',  # DD-MM-YYYY or DD/MM/YYYY
            r'(\d{4})[-_]?([a-zA-Z]{3,9})[-_]?(\d{1,2})',  # YYYY-Month-DD or YYYY_Month_DD
        ]
        
        for pattern in date_patterns:
            match = re.search(pattern, url)
            if match:
                groups = match.groups()
                if len(groups[0]) == 4:  # YYYY-MM-DD format
                    year, month, day = groups
                    try:
                        month_num = int(month)
                        if 1 <= month_num <= 12:
                            month_name = datetime.date(1, month_num, 1).strftime('%B')
                            return f"{day} {month_name} {year}"
                    except ValueError:
                        # If month is already a name
                        return f"{day} {month} {year}"
                else:  # DD-MM-YYYY format
                    day, month, year = groups
                    try:
                        month_num = int(month)
                        if 1 <= month_num <= 12:
                            month_name = datetime.date(1, month_num, 1).strftime('%B')
                            return f"{day} {month_name} {year}"
                    except ValueError:
                        return f"{day} {month} {year}"
        return None

    def extract_paper_type(self, url, snippet):
        """Extract paper type from URL or snippet"""
        wp_patterns = [
            r'WP_', r'working[_\s-]?paper', r'research[_\s-]?paper',
            r'policy[_\s-]?brief', r'technical[_\s-]?report'
        ]
        
        # First check URL
        for pattern in wp_patterns:
            if re.search(pattern, url, re.IGNORECASE):
                if 'WP_' in url or 'working' in url.lower():
                    return 'Working Paper'
                elif 'policy' in url.lower():
                    return 'Policy Brief'
                elif 'technical' in url.lower():
                    return 'Technical Report'
                else:
                    return 'Working Paper'
        
        # Then check snippet
        for pattern in wp_patterns:
            if re.search(pattern, snippet, re.IGNORECASE):
                if re.search(r'working[_\s-]?paper', snippet, re.IGNORECASE):
                    return 'Working Paper'
                elif re.search(r'policy[_\s-]?brief', snippet, re.IGNORECASE):
                    return 'Policy Brief'
                elif re.search(r'technical[_\s-]?report', snippet, re.IGNORECASE):
                    return 'Technical Report'
                else:
                    return 'Working Paper'
        
        return None

    def generate_enhanced_prompt(self, original_text, snippet, citation_url):
        """Generate an enhanced prompt with extracted metadata"""
        enhanced_data = {}
        
        # Extract date from URL if possible
        date = self.extract_date_from_url(citation_url) if citation_url else None
        if date:
            enhanced_data['extracted_date'] = date
        
        # Extract paper type from URL or snippet
        paper_type = self.extract_paper_type(citation_url, snippet) if citation_url and snippet else None
        if paper_type:
            enhanced_data['paper_type'] = paper_type
        
        # For NITI Aayog-specific formatting
        if citation_url and 'niti.gov.in' in citation_url:
            enhanced_data['agency'] = 'NITI Aayog, Government of India'
            enhanced_data['place'] = 'New Delhi'
            
            # If it's a working paper but not explicitly stated
            if 'paper_type' not in enhanced_data:
                enhanced_data['paper_type'] = 'Working Paper'
                
            # Further specify NITI Working Paper
            if enhanced_data.get('paper_type') == 'Working Paper':
                enhanced_data['paper_type'] = 'NITI Working Paper'
        
        # Convert enhanced data to string format for prompt
        enhanced_str = ""
        for key, value in enhanced_data.items():
            enhanced_str += f"{key}: {value}\n"
        
        return enhanced_str

    def extract_corrected_content(self, txt):
        """Extract the corrected content from LLM response"""
        # Look for "Formatted Footnote:" header
        if "Formatted Footnote:" in txt:
            content = txt.split("Formatted Footnote:", 1)[1].strip()
            # Remove any trailing notes or explanations
            if "\n\n" in content:
                content = content.split("\n\n")[0].strip()
            return content
        
        # Fallback extraction method
        lines = txt.strip().splitlines()
        skip_patterns = [
            "i apologize", "sorry", "without a meaningful search result",
            "insufficient to verify", "is incomplete", "note:",
            "warning:", "corrected:", "formatted:", "```"
        ]
        result = []
        for line in lines:
            line = line.strip()
            if any(p in line.lower() for p in skip_patterns):
                continue
            if line:
                result.append(line)
        return ' '.join(result).strip()

    def process_part(self, part_path):
        """Process a specific XML part (footnotes or endnotes)"""
        full_path = os.path.join(self.tmp_dir, *part_path.split("/"))
        if not os.path.exists(full_path):
            print(f"⚠️ Missing part: {part_path}")
            return

        print(f"🔧 Processing {part_path}")
        parser = ET.XMLParser(remove_blank_text=False)
        tree = ET.parse(full_path, parser)
        root = tree.getroot()

        # Determine starting rel_id_counter based on existing rels
        rels_path = os.path.join(self.tmp_dir, 'word', '_rels', f"{os.path.basename(part_path)}.rels")
        if os.path.exists(rels_path):
            rels_tree = ET.parse(rels_path)
            rels_root = rels_tree.getroot()
            existing_ids = [
                int(rel.get('Id')[3:])
                for rel in rels_root.findall('.//{http://schemas.openxmlformats.org/package/2006/relationships}Relationship')
                if rel.get('Id').startswith('rId')
            ]
            max_id = max(existing_ids) if existing_ids else 0
        else:
            max_id = 0
        rel_id_counter = max_id + 1
        rels = []

        notes = root.xpath("//w:footnote | //w:endnote", namespaces=self.ns)
        note_elements = []
        for note in notes:
            nid = note.get(f"{{{self.W}}}id")
            if nid in ("-1", "0"):
                continue
            texts = note.xpath(".//w:t", namespaces=self.ns)
            original = "".join(t.text or "" for t in texts).strip()
            if original:
                note_elements.append({'id': int(nid), 'element': note, 'text': original})

        note_elements.sort(key=lambda x: x['id'])

        for idx, data in enumerate(note_elements, 1):
            note = data['element']
            original = data['text']
            print(f"[PROCESSING] Note ID: {['id']}")
            print(f"[ORIGINAL] {original}")
            
            # Debug: show input to Tavily
            print(f"[TAVILY INPUT] Original text: {original}")
            
            # Run Tavily search
            snippet = ''
            citation_url = ''
            try:
                search_results = self.search_tool.run(original)
                if search_results:
                    # Combine up to 500 words from results
                    combined_content = []
                    word_count = 0
                    for result in search_results[:4]:  # Limit to 3-4 results
                        content = result.get('content', '') if isinstance(result, dict) else str(result)
                        url = result.get('url', '') if isinstance(result, dict) else ''
                        words = content.split()
                        for word in words:
                            if word_count < 500:
                                combined_content.append(word)
                                word_count += 1
                            else:
                                break
                        if word_count >= 500:
                            break
                        # Use the first non-DOI URL as citation if available
                        if url and not citation_url and not url.startswith('http://dx.doi.org'):
                            citation_url = url
                    snippet = ' '.join(combined_content).strip()[:1500]  # Limit snippet length
                    if len(snippet) > 1000:
                        snippet = snippet[:1000] + '...'
            except Exception as e:
                print(f"[SEARCH ERROR] {e}")

            # Debug: show result from Tavily
            print(f"[TAVILY RESULT] Snippet from Tavily: {snippet}")
            print(f"[TAVILY CITATION] URL: {citation_url}")
            
            # Generate enhanced prompt with extracted metadata
            enhanced_metadata = self.generate_enhanced_prompt(original, snippet, citation_url)
            print(f"[ENHANCED METADATA] {enhanced_metadata}")
            
            # Prepare LLM messages with enhanced metadata
            try:
                messages = self.prompt.format_messages(
                    footnote=original, 
                    snippets=f"{snippet}\n\n{enhanced_metadata}"
                )
                llm_out = self.claude_llm.invoke(messages)
                corrected = llm_out.content
                # Debug: raw LLM output
                print(f"[LLM OUTPUT] Raw LLM corrected text: {corrected}")
                corrected_text = self.extract_corrected_content(corrected)
                # Check if corrected_text already contains a URL
                url_pattern = r'https?://[^\s)]+'
                has_url = re.search(url_pattern, corrected_text)
                # Append citation URL only if no URL is present and citation_url is valid
                if citation_url and not has_url:
                    corrected_text += f" {citation_url}"
                # Debug: cleaned corrected text
                print(f"[LLM CLEANED] Extracted corrected_text: {corrected_text}")
            except Exception as e:
                print(f"[LLM ERROR] {e} — using original")
                corrected_text = original
                # Append citation URL only if no URL is present in original
                url_pattern = r'https?://[^\s)]+'
                has_url = re.search(url_pattern, corrected_text)
                if citation_url and not has_url:
                    corrected_text += f" {citation_url}"

            # Debug: show changes
            print(f"[CHANGE] Original -> Corrected: '{original}' -> '{corrected_text}'")

            # Save debug files and rebuild footnote element
            for child in list(note):
                note.remove(child)
            p = ET.SubElement(note, f"{{{self.W}}}p")
            sup = ET.SubElement(p, f"{{{self.W}}}r")
            rPr = ET.SubElement(sup, f"{{{self.W}}}rPr")
            rFonts = ET.SubElement(rPr, f"{{{self.W}}}rFonts")
            rFonts.set(f"{{{self.W}}}ascii", "Calibri")
            rFonts.set(f"{{{self.W}}}hAnsi", "Calibri")
            ET.SubElement(rPr, f"{{{self.W}}}vertAlign").set(f"{{{self.W}}}val", "superscript")
            # Set font size for superscript (10pt = 20 half-points)
            ET.SubElement(rPr, f"{{{self.W}}}sz").set(f"{{{self.W}}}val", "20")

            # Correctly create the text node and set xml:space="preserve"
            t_sup = ET.SubElement(sup, f"{{{self.W}}}t")
            t_sup.set(f"{{{self.XML}}}space", "preserve")
            t_sup.text = str(idx)
            space = ET.SubElement(p, f"{{{self.W}}}r")
            space_rPr = ET.SubElement(space, f"{{{self.W}}}rPr")
            space_fonts = ET.SubElement(space_rPr, f"{{{self.W}}}rFonts")
            space_fonts.set(f"{{{self.W}}}ascii", "Calibri")
            space_fonts.set(f"{{{self.W}}}hAnsi", "Calibri")
            # Set font size for space
            ET.SubElement(space_rPr, f"{{{self.W}}}sz").set(f"{{{self.W}}}val", "20")
            t_space = ET.SubElement(space, f"{{{self.W}}}t")
            t_space.set(f"{{{self.XML}}}space", "preserve")
            t_space.text = " "

            segments = self.parse_markdown_segments(corrected_text)
            for seg in segments:
                if not seg['text'].strip() and seg['type'] != 'hyperlink':
                    continue
                if seg['type'] == 'hyperlink':
                    rid = f"rId{rel_id_counter}"
                    link = ET.SubElement(p, f"{{{self.W}}}hyperlink")
                    link.set(f"{{{self.R}}}id", rid)
                    run = ET.SubElement(link, f"{{{self.W}}}r")
                    rPr2 = ET.SubElement(run, f"{{{self.W}}}rPr")
                    rFonts2 = ET.SubElement(rPr2, f"{{{self.W}}}rFonts")
                    rFonts2.set(f"{{{self.W}}}ascii", "Calibri")
                    rFonts2.set(f"{{{self.W}}}hAnsi", "Calibri")
                    ET.SubElement(rPr2, f"{{{self.W}}}rStyle").set(f"{{{self.W}}}val", "Hyperlink")
                    # Set font size for hyperlink
                    ET.SubElement(rPr2, f"{{{self.W}}}sz").set(f"{{{self.W}}}val", "20")
                    t2 = ET.SubElement(run, f"{{{self.W}}}t")
                    t2.set(f"{{{self.XML}}}space", "preserve")
                    t2.text = seg['text']
                    rels.append((rid, seg['text']))
                    rel_id_counter += 1
                else:
                    run = ET.SubElement(p, f"{{{self.W}}}r")
                    rPr3 = ET.SubElement(run, f"{{{self.W}}}rPr")
                    rFonts3 = ET.SubElement(rPr3, f"{{{self.W}}}rFonts")
                    rFonts3.set(f"{{{self.W}}}ascii", "Calibri")
                    rFonts3.set(f"{{{self.W}}}hAnsi", "Calibri")
                    if seg['type'] == 'italic':
                        ET.SubElement(rPr3, f"{{{self.W}}}i")
                    # Set font size for plain or italic text
                    ET.SubElement(rPr3, f"{{{self.W}}}sz").set(f"{{{self.W}}}val", "20")
                    t3 = ET.SubElement(run, f"{{{self.W}}}t")
                    t3.set(f"{{{self.XML}}}space", "preserve")
                    t3.text = seg['text']

        if rels:
            self.update_relationships(part_path, rels)
        tree.write(full_path, pretty_print=True, xml_declaration=True, encoding="UTF-8")
        print(f"✅ Saved modified: {part_path}")

    def process(self, input_path=None, output_path=None):
        if input_path:
            self.set_paths(input_path, output_path)
        if not self.input_path:
            raise ValueError("Input path is required.")
        base, ext = os.path.splitext(self.input_path)
        if not self.output_path:
            self.output_path = f"{base}_corrected{ext}"

        shutil.rmtree(self.debug_folder, ignore_errors=True)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.makedirs(self.debug_folder, exist_ok=True)
        os.makedirs(self.tmp_dir, exist_ok=True)

        self.extract_docx()
        for part in ("word/footnotes.xml", "word/endnotes.xml"):
            self.process_part(part)
        self.repackage_docx()
        shutil.rmtree(self.tmp_dir)
        return self.output_path

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python footnote_corrector.py <input.docx> [<output.docx>]")
        sys.exit(1)
    inp = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    proc = DocxFootnoteProcessor(input_path=inp, output_path=out)
    newfile = proc.process()
    print(f"🎉 Done! New file: {newfile}")