import json
import re
import os
from pathlib import Path
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from docx import Document
from dotenv import load_dotenv

class VariantScanner:
    """A class to scan .docx documents for geopolitical and organizational entity variants."""
    
    def __init__(self):
        """Initialize the VariantScanner with environment variables and model configuration."""
        load_dotenv()
        
        # Verify API key
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in .env file or environment variables")
        
        # Model configuration
        self.model_name = "claude-3-5-haiku-20241022"
        self.llm = ChatAnthropic(
            model_name=self.model_name,
            api_key=self.api_key,
            temperature=0
        )
        
        # System prompt for SCAN
        self.system_prompt = """
ROLE:
You are a JSON-only assistant specializing in the normalization of geopolitical and organizational entities.

TASK:
Read the entire USER document (from BEGIN to END markers). Identify every mention of a country or organization (including acronyms or nicknames). For each such entity, collect and return all surface form variants that refer to the same real-world country or organization.

DO NOT:
- Do not return adjectival or demonym forms (e.g., Indian, Russian, Indonesian) in the output JSON.
- Do not include purely technical acronyms (e.g., AI, IoT) unless they clearly represent a country or organization.
- Do not include person names, cities, or non-country/non-organization entities (e.g., New Delhi, Kathmandu, Beijing, Dhaka are cities, not countries).
- Do not include invented or hallucinated variants that are not present in the input.
- Do not include any text, explanations, or commentary outside the JSON object.
- Do not extract entities that appear in only one form (i.e., no variants).
- Do not return anything except a single valid JSON object.

CHAIN-OF-THOUGHT PROCESS (for internal use, do not include in output):
1. Parse the full text between BEGIN and END markers.
2. Identify all mentions of countries or organizations (e.g., India, United Nations, DPRK).
3. Collect all surface forms referring to the same entity — this includes:
   - Full official names (e.g., Democratic People’s Republic of Korea)
   - Common names (e.g., North Korea)
   - Abbreviations and acronyms (e.g., DPRK, UN)
4. Discard any form that is an adjectival or demonym variant (e.g., Indian, Japanese, Indonesian, Russian, Chinese).
5. Exclude geographic descriptors or regions that are not sovereign entities (e.g., Indo-Pacific, Middle East).
6. Exclude cities (e.g., New Delhi, Kathmandu, Beijing, Dhaka).
7. Only include entities that have at least two valid surface form variants.
8. Return a single JSON object in this format:
   {
     "<entity_key>": ["<variant1>", "<variant2>", ...],
     ...
   }

OUTPUT:
Return only a single valid JSON object. No additional text, explanations, or commentary.
"""

    def call_scan(self, full_document: str) -> dict:
        """
        Call the LLM to scan the document for entity variants.
        
        Args:
            full_document (str): The full text of the document to scan.
        
        Returns:
            dict: A dictionary mapping entities to their variant forms.
        
        Raises:
            ValueError: If the model response cannot be parsed as JSON.
        """
        # Prepare messages
        messages = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=f"<<DOC-START>>\n{full_document}\n<<DOC-END>>")
        ]
        
        # Invoke the model
        response = self.llm.invoke(messages)
        
        # Extract JSON from response (remove leading/trailing non-JSON text)
        json_match = re.search(r'\{.*\}', response.content, re.DOTALL)
        if not json_match:
            raise ValueError(f"No JSON found in response: {response.content}")
        
        json_str = json_match.group(0)
        try:
            variant_map = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse extracted JSON: {e}. Raw response: {response.content}")
        
        # Print a preview of the result
        print("◾ SCAN result:", json.dumps(variant_map, indent=2)[:500], "...\n")
        
        return variant_map

    def process(self, input_path: str, output_path: str) -> None:
        """
        Read a .docx document, scan for variant map, and save the result to a JSON file.
        
        Args:
            input_path (str): Path to the input .docx document.
            output_path (str): Path to save the output JSON file.
        
        Raises:
            FileNotFoundError: If the input file does not exist.
            ValueError: If the input file is not a .docx file, the document is empty, 
                       or reading/parsing fails.
        """
        # Convert paths to strings for consistency
        input_path = str(Path(input_path))
        output_path = str(Path(output_path))
        
        # Validate input file
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")
        if not input_path.lower().endswith('.docx'):
            raise ValueError(f"Input file must be a .docx file: {input_path}")
        
        # Read the .docx document
        try:
            doc = Document(input_path)
            document = "\n".join([para.text for para in doc.paragraphs if para.text.strip()])
        except Exception as e:
            raise ValueError(f"Failed to read .docx file: {e}")
        
        if not document.strip():
            raise ValueError("Document is empty or contains no readable text")
        
        # Call the scan function
        variant_map = self.call_scan(document)
        
        # Ensure output directory exists
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        
        # Save the variant map to JSON
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(variant_map, f, indent=2, ensure_ascii=False)
        
        print(f"◾ Variant map saved to: {output_path}")

if __name__ == "__main__":
    # Example usage
    scanner = VariantScanner()
    try:
        scanner.process("SouthFull.docx", "variant_map.json")
    except Exception as e:
        print(f"Error: {e}")