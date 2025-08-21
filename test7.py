import gradio as gr
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.predefined_recognizers import SpacyRecognizer
from presidio_analyzer.nlp_engine import SpacyNlpEngine
from docx import Document
from odf.opendocument import load as load_odf
from odf.text import P

# ------------------ spaCy + Presidio Initialization ------------------ #

# Configure and load spaCy NLP engine with large model
nlp_config = {"en": {"model": "en_core_web_lg", "pipeline": ["tok2vec", "ner"]}}
nlp_engine = SpacyNlpEngine(nlp_configuration=nlp_config)
nlp_engine.load()

# Initialize Analyzer and attach spaCy-based recognizer
analyzer = AnalyzerEngine()
spacy_recognizer = SpacyRecognizer(supported_language="en", nlp_engine=nlp_engine)
analyzer.registry.add_recognizer(spacy_recognizer)

# ------------------ Document Reading Utilities ------------------ #

def read_docx_text(file_path):
    doc = Document(file_path)
    return "\n".join([para.text for para in doc.paragraphs])

def read_odt_text(file_path):
    odt = load_odf(file_path)
    paragraphs = odt.getElementsByType(P)
    return "\n".join([str(p) for p in paragraphs])

# ------------------ Name Extraction Logic ------------------ #

def extract_presidio_names(text):
    results = analyzer.analyze(text=text, language="en")
    person_names = [text[result.start:result.end] for result in results if result.entity_type == "PERSON"]
    return list(set(person_names))

# ------------------ Gradio Interface ------------------ #

def process_document(file):
    file_path = file.name
    try:
        if file_path.endswith(".docx"):
            text = read_docx_text(file_path)
        elif file_path.endswith(".odt"):
            text = read_odt_text(file_path)
        else:
            return "Unsupported file type. Please upload .docx or .odt only."

        names = extract_presidio_names(text)
        return "\n".join(names) if names else "No names detected."

    except Exception as e:
        return f"Error: {str(e)}"

iface = gr.Interface(
    fn=process_document,
    inputs=gr.File(label="Upload DOCX or ODT Document", file_types=[".docx", ".odt"]),
    outputs="textbox",
    title="🇮🇳 Indian Name Detector using Microsoft Presidio",
    description="Detects real human names from uploaded documents using spaCy + Microsoft Presidio. Filters out countries, schemes like 'Viksit Bharat', and organizations."
)

iface.launch()
