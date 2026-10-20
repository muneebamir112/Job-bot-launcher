import io
import os
import re
import html
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive"
]

DEFAULT_FOLDER_NAME = "Job Bot Question Docs"

def get_drive_service(credentials_path: str = None, token_path: str = None, service_account_path: str = None):
    """
    Returns an authenticated Google Drive v3 service.
    Prefers OAuth user token (token.json) which has native Drive creation capabilities,
    and falls back to service_account.json.
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    token_file = token_path or os.path.join(current_dir, "token.json")
    sa_file = service_account_path or os.path.join(current_dir, "service_account.json")

    # 1. Try OAuth token.json
    if os.path.exists(token_file):
        try:
            creds = UserCredentials.from_authorized_user_file(token_file, SCOPES)
            return build("drive", "v3", credentials=creds, static_discovery=False, cache_discovery=False)
        except Exception as e:
            print(f"[Drive] Warning: could not initialize with token.json: {e}")

    # 2. Fallback to service_account.json
    if os.path.exists(sa_file):
        try:
            creds = ServiceCredentials.from_service_account_file(sa_file, scopes=SCOPES)
            return build("drive", "v3", credentials=creds, static_discovery=False, cache_discovery=False)
        except Exception as e:
            print(f"[Drive] Warning: could not initialize with service_account.json: {e}")

    raise RuntimeError("No valid token.json or service_account.json found for Google Drive API.")


def get_or_create_questions_folder(drive_service, folder_name: str = DEFAULT_FOLDER_NAME) -> str:
    """Finds or creates a folder in Google Drive to hold all per-job question docs."""
    query = f"mimeType = 'application/vnd.google-apps.folder' and name = '{folder_name}' and trashed = false"
    try:
        res = drive_service.files().list(q=query, spaces='drive', fields='files(id, name)', pageSize=10).execute()
        files = res.get('files', [])
        if files:
            return files[0]['id']
        
        meta = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        created = drive_service.files().create(body=meta, fields='id').execute()
        return created['id']
    except Exception as e:
        print(f"[Drive] Warning finding/creating folder '{folder_name}': {e}")
        return ""


def get_existing_question_docs(drive_service, folder_id: str = None) -> dict:
    """
    Returns a dictionary of {doc_name_lower: doc_id} for all existing question docs.
    """
    existing = {}
    try:
        if folder_id:
            query = f"mimeType = 'application/vnd.google-apps.document' and '{folder_id}' in parents and trashed = false"
        else:
            query = f"mimeType = 'application/vnd.google-apps.document' and trashed = false"
            
        page_token = None
        while True:
            res = drive_service.files().list(
                q=query,
                spaces='drive',
                fields='nextPageToken, files(id, name)',
                pageSize=1000,
                pageToken=page_token
            ).execute()
            for f in res.get('files', []):
                name = f.get('name', '').strip().lower()
                if name:
                    existing[name] = f.get('id', '')
            page_token = res.get('nextPageToken')
            if not page_token:
                break
    except Exception as e:
        print(f"[Drive] Warning reading existing question docs: {e}")
    return existing


def create_job_question_doc(
    company_name: str,
    job_title: str,
    job_url: str,
    questions: list,
    drive_service=None,
    folder_name: str = DEFAULT_FOLDER_NAME
) -> dict:
    """
    Creates a new standalone Google Doc named '{company} - {job_title}',
    containing all scraped application questions and options, and places it
    inside the Google Drive 'Job Bot Question Docs' folder.
    
    Returns a dict with {'id': doc_id, 'url': webViewLink, 'name': doc_name}.
    """
    if not questions:
        return {}

    if not drive_service:
        drive_service = get_drive_service()

    clean_comp = (company_name or "Unknown Company").strip()
    clean_title = (job_title or "Job").strip()
    doc_name = f"{clean_comp} - {clean_title}"

    # Get destination folder
    folder_id = get_or_create_questions_folder(drive_service, folder_name)

    # Build clean HTML content that Google Drive converts directly into formatted Docs
    html_parts = [
        "<html><head><meta charset='utf-8'></head><body>",
        f"<h1 style='color: #1a73e8; margin-bottom: 4px;'>{html.escape(doc_name)}</h1>",
        f"<p style='color: #555555; margin-top: 0;'>",
        f"<b>Company:</b> {html.escape(clean_comp)}<br/>",
        f"<b>Job Title:</b> {html.escape(clean_title)}<br/>",
        f"<b>Application URL:</b> <a href='{html.escape(job_url)}'>{html.escape(job_url)}</a><br/>",
        f"<b>Total Form Questions:</b> {len(questions)}",
        "</p>",
        "<hr style='border: 0; border-top: 1px solid #cccccc; margin: 16px 0;'/>",
        "<h3>Application Form Questions:</h3>",
        "<ol style='padding-left: 20px;'>"
    ]

    for q_item in questions:
        if isinstance(q_item, dict):
            q_text = q_item.get("question", "").strip()
            opts = q_item.get("options", [])
        else:
            q_text = str(q_item).strip()
            opts = []

        html_parts.append("<li style='margin-bottom: 14px;'>")
        html_parts.append(f"<p style='font-size: 11pt; font-weight: bold; margin: 0 0 4px 0;'>{html.escape(q_text)}</p>")
        
        if opts:
            html_parts.append("<ul style='list-style-type: disc; margin: 4px 0; padding-left: 20px;'>")
            for opt in opts:
                clean_opt = str(opt).strip()
                if clean_opt:
                    html_parts.append(f"<li style='font-size: 10pt; color: #333333;'>{html.escape(clean_opt)}</li>")
            html_parts.append("</ul>")

        html_parts.append("</li>")

    html_parts.append("</ol>")
    html_parts.append("</body></html>")
    full_html = "".join(html_parts)

    media = MediaIoBaseUpload(io.BytesIO(full_html.encode("utf-8")), mimetype="text/html", resumable=False)
    
    file_metadata = {
        "name": doc_name,
        "mimeType": "application/vnd.google-apps.document"
    }
    if folder_id:
        file_metadata["parents"] = [folder_id]

    created_file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, name, webViewLink, webContentLink"
    ).execute()

    doc_id = created_file.get("id")
    web_link = created_file.get("webViewLink")

    # Set permissions so anyone with the link can view/edit
    try:
        drive_service.permissions().create(
            fileId=doc_id,
            body={"type": "anyone", "role": "writer"},
            fields="id"
        ).execute()
    except Exception as perm_err:
        try:
            drive_service.permissions().create(
                fileId=doc_id,
                body={"type": "anyone", "role": "reader"},
                fields="id"
            ).execute()
        except Exception:
            pass

    return {
        "id": doc_id,
        "url": web_link,
        "name": doc_name
    }


# Backwards compatibility helpers
def get_existing_companies(service_account_file: str, doc_id: str) -> set:
    try:
        drive = get_drive_service(service_account_path=service_account_file)
        docs = get_existing_question_docs(drive)
        # return company prefixes
        return {k.split(" - ")[0].strip().lower() for k in docs.keys()}
    except Exception:
        return set()

def append_company_questions(service_account_file: str, doc_id: str, company_name: str, questions: list) -> bool:
    res = create_job_question_doc(company_name, "Application", "", questions)
    return bool(res.get("id"))
