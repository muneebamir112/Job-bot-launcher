import os
import sys
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# If modifying these scopes, delete token.json.
SCOPES = ['https://www.googleapis.com/auth/drive']

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(CURRENT_DIR)
DEFAULT_CREDENTIALS = os.path.join(CURRENT_DIR, "credentials.json")
DEFAULT_TOKEN = os.path.join(CURRENT_DIR, "token.json")
DEFAULT_CVS_DIR = os.getenv("RESUMES_SAVE_PATH", os.path.join(BASE_DIR, "CVs"))
DEFAULT_FOLDER_NAME = "Job Bot CVs"


def get_drive_service(credentials_path: str = DEFAULT_CREDENTIALS, token_path: str = DEFAULT_TOKEN):
    """Authenticates the user using OAuth 2.0 and returns the Google Drive service.
    
    Opens the default browser for authorization on the first run and persists
    the session in token.json for all subsequent automated runs.
    """
    creds = None
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception as e:
            print(f"[Drive] Warning: Could not load existing token: {e}")
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                print("[Drive] Refreshing expired OAuth token...")
                creds.refresh(Request())
            except Exception as e:
                print(f"[Drive] Failed to refresh token ({e}), re-authenticating...")
                creds = None

        if not creds:
            if not os.path.exists(credentials_path):
                raise FileNotFoundError(
                    f"Google Drive OAuth credentials file not found at: {credentials_path}\n"
                    "Please ensure credentials.json is saved in the Job-bot-launcher folder."
                )
            print("[Drive] Initiating browser login for Google Drive...")
            print("[Drive] A browser tab will open. Please sign in and allow access to Google Drive.")
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)

        # Save credentials for future executions
        try:
            with open(token_path, 'w', encoding='utf-8') as token_file:
                token_file.write(creds.to_json())
            print(f"[Drive] Saved authentication token to {token_path}")
        except Exception as e:
            print(f"[Drive] Warning: Could not save token.json: {e}")

    return build('drive', 'v3', credentials=creds)


def get_or_create_folder(service, folder_name: str = DEFAULT_FOLDER_NAME, parent_id: str = None) -> str:
    """Finds an existing folder by name on Google Drive or creates it if it doesn't exist."""
    query = f"mimeType = 'application/vnd.google-apps.folder' and name = '{folder_name}' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"

    try:
        results = service.files().list(
            q=query,
            spaces='drive',
            fields='files(id, name)',
            pageSize=10
        ).execute()
        files = results.get('files', [])

        if files:
            folder_id = files[0]['id']
            print(f"[Drive] Found existing folder '{folder_name}' (ID: {folder_id})")
            return folder_id

        # Folder does not exist, create it
        print(f"[Drive] Folder '{folder_name}' not found. Creating it on Google Drive...")
        folder_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        if parent_id:
            folder_metadata['parents'] = [parent_id]

        folder = service.files().create(body=folder_metadata, fields='id').execute()
        folder_id = folder.get('id')
        print(f"[Drive] Successfully created folder '{folder_name}' (ID: {folder_id})")
        return folder_id

    except Exception as e:
        print(f"[Drive] Error getting/creating folder '{folder_name}': {e}")
        raise


def upload_file_to_drive(service, file_path: str, folder_id: str, overwrite: bool = True) -> dict:
    """Uploads a local file to the specified Google Drive folder.
    
    If a file with the same name already exists in the folder and overwrite=True,
    its content is updated.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File to upload not found: {file_path}")

    file_name = os.path.basename(file_path)
    
    # Check if file with same name already exists in this folder
    query = f"name = '{file_name}' and '{folder_id}' in parents and trashed = false"
    existing = service.files().list(
        q=query,
        spaces='drive',
        fields='files(id, name, webViewLink)',
        pageSize=5
    ).execute().get('files', [])

    media = MediaFileUpload(file_path, mimetype='application/pdf', resumable=True)

    if existing:
        if overwrite:
            existing_id = existing[0]['id']
            print(f"[Drive] Updating existing file '{file_name}'...")
            updated = service.files().update(
                fileId=existing_id,
                media_body=media,
                fields='id, name, webViewLink'
            ).execute()
            print(f"[Drive] Updated: {file_name} -> {updated.get('webViewLink', '')}")
            return updated
        else:
            print(f"[Drive] File '{file_name}' already exists in folder, skipping.")
            return existing[0]

    # Create new file
    file_metadata = {
        'name': file_name,
        'parents': [folder_id]
    }
    created = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id, name, webViewLink'
    ).execute()
    print(f"[Drive] Uploaded: {file_name} -> {created.get('webViewLink', '')}")
    return created


def upload_single_cv(file_path: str, folder_name: str = DEFAULT_FOLDER_NAME) -> dict:
    """Uploads a single CV or cover letter to the Google Drive folder."""
    service = get_drive_service()
    folder_id = get_or_create_folder(service, folder_name=folder_name)
    return upload_file_to_drive(service, file_path, folder_id, overwrite=True)


def upload_all_cvs(cvs_dir: str = DEFAULT_CVS_DIR, folder_name: str = DEFAULT_FOLDER_NAME, overwrite: bool = True):
    """Scans the CVs directory and uploads all PDF files to the Google Drive folder."""
    if not os.path.exists(cvs_dir):
        print(f"[Drive] CV directory does not exist: {cvs_dir}")
        return 0

    pdf_files = [f for f in os.listdir(cvs_dir) if f.lower().endswith('.pdf')]
    if not pdf_files:
        print(f"[Drive] No PDF files found in {cvs_dir}")
        return 0

    print(f"[Drive] Found {len(pdf_files)} PDF file(s) in {cvs_dir}. Connecting to Google Drive...")
    service = get_drive_service()
    folder_id = get_or_create_folder(service, folder_name=folder_name)

    success_count = 0
    for idx, fname in enumerate(pdf_files, start=1):
        full_path = os.path.join(cvs_dir, fname)
        print(f"[{idx}/{len(pdf_files)}] Uploading {fname}...")
        try:
            upload_file_to_drive(service, full_path, folder_id, overwrite=overwrite)
            success_count += 1
        except Exception as e:
            print(f"  [Error] Failed to upload {fname}: {e}")

    print(f"\n[Drive] Done! Successfully uploaded {success_count}/{len(pdf_files)} files to Google Drive folder '{folder_name}'.")
    return success_count


if __name__ == "__main__":
    cvs_directory = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CVS_DIR
    target_folder = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_FOLDER_NAME
    upload_all_cvs(cvs_dir=cvs_directory, folder_name=target_folder)
