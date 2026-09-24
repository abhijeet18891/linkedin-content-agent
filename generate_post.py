import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google import genai

# 1. Authenticate with Google Drive
def get_drive_service():
    service_account_info = json.loads(os.environ["GCP_SERVICE_ACCOUNT_KEY"])
    creds = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=['https://www.googleapis.com/auth/drive.readonly']
    )
    return build('drive', 'v3', credentials=creds)

# 2. Resolve Shortcuts & Read File Contents
def read_drive_file_content(service, file_id, mime_type, shortcut_details=None):
    if mime_type == 'application/vnd.google-apps.shortcut':
        target_id = shortcut_details.get('targetId')
        target_meta = service.files().get(
            fileId=target_id,
            fields='id, mimeType, shortcutDetails',
            supportsAllDrives=True
        ).execute()
        return read_drive_file_content(
            service,
            target_meta['id'],
            target_meta['mimeType'],
            target_meta.get('shortcutDetails')
        )

    if mime_type == 'application/vnd.google-apps.document':
        response = service.files().export(fileId=file_id, mimeType='text/plain').execute()
        return response.decode('utf-8')

    if mime_type in ['text/plain', 'text/markdown', 'application/json']:
        response = service.files().get_media(fileId=file_id).execute()
        return response.decode('utf-8')

    return ""

def fetch_all_sources(folder_id):
    service = get_drive_service()
    query = f"'{folder_id}' in parents and trashed = false"
    results = service.files().list(
        q=query,
        fields="files(id, name, mimeType, shortcutDetails)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()

    files = results.get('files', [])
    aggregated_context = ""

    for f in files:
        content = read_drive_file_content(
            service,
            f['id'],
            f['mimeType'],
            f.get('shortcutDetails')
        )
        if content.strip():
            aggregated_context += f"\n\n--- DOCUMENT: {f['name']} ---\n{content}"

    return aggregated_context

# 3. Generate Draft Post via Gemini
def generate_draft():
    folder_id = os.environ["DRIVE_FOLDER_ID"]
    context_notes = fetch_all_sources(folder_id)

    if not context_notes.strip():
        print("No readable documents or shortcuts found in the Drive folder.")
        return

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    system_instruction = """
    You are an expert ghostwriter and Senior Product Design / Design Systems Leader.
    Your task is to write a high-engagement LinkedIn post based on real field insights, case studies, and notes.

    Tone & Persona:
    - Grounded, insightful peer; zero corporate fluff, cliches, or synthetic optimism.
    - Focus on practical systems, architecture trade-offs, mobile/fintech execution, and UX friction.

    Template Rules:
    1. Hook: 1-2 lines. Counter-intuitive statement, surprising metric, or sharp observation.
    2. Context: The hidden friction or trap most teams fall into.
    3. The Takeaway: 3-4 bullet points breaking down the framework or fix.
    4. Outro: A single open question encouraging meaningful peer discussion.

    Output Format (Strict):
    ### LINKEDIN_POST
    [Full LinkedIn post content]

    ### IMAGE_ONE_LINER
    [Exactly one punchy headline / one-liner to drop onto a pre-designed visual banner or carousel cover]
    """

    prompt = f"Here is the knowledge base extracted from my Google Drive documents:\n{context_notes}\n\nPick a compelling theme or friction point and craft this morning's post."

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={"system_instruction": system_instruction}
    )

    print(response.text)

    os.makedirs("drafts", exist_ok=True)
    with open("drafts/latest_post.md", "w", encoding="utf-8") as out:
        out.write(response.text)

if __name__ == "__main__":
    generate_draft()
