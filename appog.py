import os
import shutil
import tempfile
import time
import gradio as gr
import asyncio
from doc import DocumentProcessor
from t import TextCorrector
from footnote_corrector import DocxFootnoteProcessor
from formatter import DocxFormatterDetector, DocxBackToBoldItalic
from abbre import AbbreviationExpander
from variantScanner import VariantScanner
from langchain_anthropic import ChatAnthropic
from dotenv import load_dotenv

def count_footnotes(docx_path):
    """Count footnotes in a docx file"""
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            shutil.unpack_archive(docx_path, tmpdir, 'zip')
            footnotes_path = os.path.join(tmpdir, 'word', 'footnotes.xml')
            if os.path.exists(footnotes_path):
                import xml.etree.ElementTree as ET
                tree = ET.parse(footnotes_path)
                root = tree.getroot()
                ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                notes = root.findall('.//w:footnote', ns)
                return len([n for n in notes if n.get(f"{{{ns['w']}}}id") not in ("-1", "0")])
            return 0
        except shutil.ReadError as e:
            print(f"[ERROR] Failed to count footnotes in {docx_path}: {e}")
            return 0

def build_llm():
    load_dotenv()
    return ChatAnthropic(
        temperature=0.2,
        anthropic_api_key=os.getenv('ANTHROPIC_API_KEY'),
        model_name='claude-3-7-sonnet-20250219'
    )

class PipelineRunner:
    def __init__(self, original_path: str, final_output_path: str):
        self.original_path = original_path
        self.final_output_path = final_output_path
        self.llm = build_llm()

    async def run(self, progress=None):
        log_messages = []
        
        def log(message):
            log_messages.append(message)
            print(message)
            if progress:
                progress(message, len(log_messages))
        
        with tempfile.TemporaryDirectory() as tmpdirname:
            # Count initial footnotes
            initial_footnotes = count_footnotes(self.original_path)
            log(f"[INFO] 🔢 Initial footnote count: {initial_footnotes}")

            # Step 1: Copy original to temporary directory
            step0 = os.path.join(tmpdirname, "step0.docx")
            shutil.copy(self.original_path, step0)
            log(f"[INFO] 📋 Step 1: Copied original to {step0}")
            log(f"[INFO] 🔢 Footnotes after copy: {count_footnotes(step0)}")

            # Step 2: Scan for entity variants (variantScanner.py)
            variant_json = os.path.join(tmpdirname, "variant_map.json")
            scanner = VariantScanner()
            scanner.process(step0, variant_json)
            log(f"[INFO] 📋 Step 2: Scanned entity variants, saved to {variant_json}")
            log(f"[INFO] 🔢 Footnotes after variant scanning: {count_footnotes(step0)}")

            # Step 3: Detect bold/italic and convert to ** and * (formatter.py)
            step3_text = os.path.join(tmpdirname, "formatted.txt")
            detector = DocxFormatterDetector()
            detector.detect_and_convert(step0, step3_text)
            log(f"[INFO] 📋 Step 3: Converted to text with asterisks saved to {step3_text}")

            # Step 4: Format document structure (doc.py)
            step1 = os.path.join(tmpdirname, "doc_formatted_1.docx")
            formatter = DocumentProcessor(step3_text, step1)
            formatter.process()
            log(f"[INFO] 📋 Step 4: Formatted document structure saved to {step1}")
            log(f"[INFO] 🔢 Footnotes after formatting: {count_footnotes(step1)}")

            # Step 5: Abbreviation expansion (abbre.py)
            stepabbrv = os.path.join(tmpdirname, "abbrev_expanded.docx")
            abbrev_expander = AbbreviationExpander(self.llm)
            await abbrev_expander.process(step1, stepabbrv, variant_map_path=variant_json)
            log(f"[INFO] 📋 Step 5: Expanded abbreviations saved to {stepabbrv}")
            log(f"[INFO] 🔢 Footnotes after abbreviation expansion: {count_footnotes(stepabbrv)}")
            
            log(f"[INFO] ⏳ Waiting 60 seconds to avoid Claude rate limit before text correction...")
            await asyncio.sleep(60)
            
            # Step 6: Text corrections (t.py)
            step2 = os.path.join(tmpdirname, "corrected.docx")
            corrector = TextCorrector(self.llm)
            await corrector.process(stepabbrv, step2)
            log(f"[INFO] 📋 Step 6: Corrected text saved to {step2}")
            log(f"[INFO] 🔢 Footnotes after text correction: {count_footnotes(step2)}")
            
            # Step 7: Format document structure again (doc.py)
            stepad = os.path.join(tmpdirname, "doc_formatted_2.docx")
            formatter = DocumentProcessor(step2, stepad)
            formatter.process()
            log(f"[INFO] 📋 Step 7: Formatted document structure saved to {stepad}")
            log(f"[INFO] 🔢 Footnotes after second formatting: {count_footnotes(stepad)}")

            # Step 8: Convert asterisks back to bold/italic (formatter.py)
            step4 = os.path.join(tmpdirname, "back_to_docx.docx")
            back_transformer = DocxBackToBoldItalic()
            back_transformer.transform(stepad, step4)
            log(f"[INFO] 📋 Step 8: Converted back to DOCX saved to {step4}")
            log(f"[INFO] 🔢 Footnotes after conversion back: {count_footnotes(step4)}")

            # Step 9: Footnote correction (footnote_corrector.py)
            # step5 = os.path.join(tmpdirname, "corrected_footnotes.docx")
            # footnote_corrector = DocxFootnoteProcessor()
            # footnote_corrector.process(step4, step5)
            # log(f"[INFO] 📋 Step 9: Footnote correction saved to {step5}")
            # log(f"[INFO] 🔢 Footnotes after footnote correction: {count_footnotes(step5)}")
            
            # Save the result as the final output
            shutil.copy(step4, self.final_output_path)
            final_footnotes = count_footnotes(self.final_output_path)
            log(f"[INFO] 📋 Final output saved to {self.final_output_path}")
            log(f"[INFO] 🔢 Final footnote count: {final_footnotes}")

            if initial_footnotes != final_footnotes:
                log(f"[WARNING] ⚠️ Footnote count changed from {initial_footnotes} to {final_footnotes}")
            else:
                log(f"[INFO] ✅ All {initial_footnotes} footnotes preserved successfully")
            
            return self.final_output_path, "\n".join(log_messages)

async def process_document(input_file, progress=gr.Progress()):
    # Create a temporary output file with a unique name
    output_filename = f"processed_{os.path.basename(input_file.name)}"
    output_path = os.path.join(tempfile.gettempdir(), output_filename)
    
    # Run the pipeline
    runner = PipelineRunner(input_file.name, output_path)
    output_file, logs = await runner.run(lambda msg, step: progress(step/8, desc=msg))
    
    return output_file, logs

# Create the Gradio interface
def create_gradio_interface():
    with gr.Blocks(title="Document Processing Pipeline") as app:
        gr.Markdown("# Document Processing Pipeline")
        gr.Markdown("Upload a DOCX file to process it through the document formatting and correction pipeline.")
        
        with gr.Row():
            with gr.Column():
                input_file = gr.File(label="Input DOCX File", file_types=[".docx"])
                process_btn = gr.Button("Process Document", variant="primary")
            
            with gr.Column():
                output_file = gr.File(label="Processed Document")
                logs = gr.Textbox(label="Processing Logs", lines=10)
        
        process_btn.click(
            fn=process_document,
            inputs=[input_file],
            outputs=[output_file, logs]
        )
    
    return app

if __name__ == '__main__':
    # Create and launch the Gradio interface
    app = create_gradio_interface()
    app.queue(default_concurrency_limit=1, max_size=2)
    app.launch(share=True)