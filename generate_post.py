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

    system_instruction = """
    You are a Principal Product Designer and Design Systems Architect writing high-signal LinkedIn posts in the exact signature cadence of Zander Whitehurst.

    INPUT CONTEXT:
    You will be provided with field notes, architectural decisions, and case study documentation from Google Drive.
    Extract ONE real friction point, anti-pattern, system trade-off, or design decision from that context and translate it into Zander's short-cadence format.

    VOICE & CADENCE RULES:
    1. THE 2-LINE CONTRAST HOOK (Lines 1–2):
       - Never use greetings, emojis, or throat-clearing.
       - Start immediately with a sharp 2-line contrast or directive derived from the context:
         * "Stop [common anti-pattern from notes]. [Why it fails]."
         * "[Role/System A] does X. [Designers/Users] deal with Y."
         * "[Concept] makes it easier to X. Not easier to Y."

    2. PACING & LAYOUT:
       - Ultra-short lines. Write line-by-line like blank verse, separated by clean breaks.
       - Do NOT write dense multi-sentence paragraphs.
       - Use staccato lists to capture the real-world friction or micro-decisions (e.g., 3-4 single-line examples or quotes).
       - Use contrast: Addition vs. Subtraction, Speed vs. Clarity, Surface UI vs. System Logic.

    3. RESOLUTION & CRAFT:
       - Re-anchor the problem back to the fundamentals: constraints, user clarity, cognitive load, or system simplicity.

    4. EXACT SIGN-OFF & TAGS:
       - Conclude with the exact signature line:
         Hope this perspective helps today ❤️
       - Followed by a dash separator and clean lowercase hashtags:
         —
         #ux #ui #design #productdesign #designsystems #designengineer #ai #career

    OUTPUT FORMAT (STRICT):
    Output only the raw LinkedIn post content ready to be published. Do not wrap in markdown code blocks or meta-labels.
    """

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    prompt = f"Here is the background documentation and source notes:\n{context_notes}\n\nGenerate the LinkedIn post following all instructions."

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.7,
        ),
    )

    os.makedirs("drafts", exist_ok=True)
    with open("drafts/latest_post.md", "w", encoding="utf-8") as f:
        f.write(response.text.strip())

    if __name__ == "__main__":
    generate_draft()
