import os
import re
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive"
]

def get_docs_service(service_account_file: str):
    if not os.path.exists(service_account_file):
        raise FileNotFoundError(f"Service account file not found: {service_account_file}")
    creds = Credentials.from_service_account_file(service_account_file, scopes=SCOPES)
    return build("docs", "v1", credentials=creds)

def get_existing_companies(service_account_file: str, doc_id: str) -> set:
    """Returns a set of lowercased company names already present in the Google Doc."""
    try:
        service = get_docs_service(service_account_file)
        doc = service.documents().get(documentId=doc_id).execute()
        body = doc.get("body", {})
        content = body.get("content", [])
        
        full_text = []
        for elem in content:
            if "paragraph" in elem:
                for p_elem in elem["paragraph"].get("elements", []):
                    if "textRun" in p_elem:
                        full_text.append(p_elem["textRun"].get("content", ""))
        
        combined = "".join(full_text)
        # Match headings like [Company Name]
        matches = re.findall(r'\[([^\]\n]+)\]', combined)
        return {m.strip().lower() for m in matches if m.strip()}
    except Exception as e:
        print(f"Error reading existing companies from Google Doc: {e}")
        return set()

def append_company_questions(service_account_file: str, doc_id: str, company_name: str, questions: list) -> bool:
    """
    Appends a company and its questions to the Google Doc:
    [COMPANY NAME]
    Q1: ...
    Q2: ...
    
    (gap)
    """
    if not questions:
        return False
    
    clean_company = company_name.strip()
    if not clean_company:
        clean_company = "Unknown Company"
        
    try:
        service = get_docs_service(service_account_file)
        doc = service.documents().get(documentId=doc_id).execute()
        body = doc.get("body", {})
        content = body.get("content", [])
        if not content:
            end_idx = 1
        else:
            end_idx = content[-1].get("endIndex", 1) - 1
            if end_idx < 1:
                end_idx = 1

        heading = f"[{clean_company}]\n"
        lines = []
        for i, item in enumerate(questions, start=1):
            if isinstance(item, dict):
                q_text = item.get("question", "").strip()
                opts = item.get("options", [])
            else:
                q_text = str(item).strip()
                opts = []
            
            lines.append(f"Q{i}: {q_text}")
            for opt in opts:
                clean_opt = str(opt).strip()
                if clean_opt:
                    lines.append(f"    - {clean_opt}")
                    
        questions_text = "\n".join(lines)
        full_block = f"{heading}{questions_text}\n\n"
        
        requests = [
            {
                "insertText": {
                    "location": {
                        "index": end_idx
                    },
                    "text": full_block
                }
            },
            {
                "updateTextStyle": {
                    "range": {
                        "startIndex": end_idx,
                        "endIndex": end_idx + len(heading) - 1
                    },
                    "textStyle": {
                        "bold": True,
                        "fontSize": {
                            "magnitude": 12,
                            "unit": "PT"
                        }
                    },
                    "fields": "bold,fontSize"
                }
            }
        ]
        
        service.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()
        return True
    except Exception as e:
        print(f"Error appending company questions to Google Doc for '{company_name}': {e}")
        return False
