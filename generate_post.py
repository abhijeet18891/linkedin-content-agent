import os
import json
import time
import random
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google import genai
from google.genai import errors as genai_errors
import httpx

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

# Retry transient Gemini errors (503 overloaded, 429 rate limit, 5xx) with backoff,
# then fall back to the next model in the list.
RETRYABLE_CODES = {429, 500, 502, 503, 504}

def generate_with_retry(client, models, prompt, config, attempts_per_model=2, base_delay=10):
    last_error = None
    for model in models:
        for attempt in range(1, attempts_per_model + 1):
            try:
                print(f"Calling {model} (attempt {attempt}/{attempts_per_model})")
                response = client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                if response.text and response.text.strip():
                    return response
                raise RuntimeError("Empty response from model")
            except (genai_errors.ServerError, genai_errors.ClientError) as e:
                code = getattr(e, "code", None)
                if isinstance(e, genai_errors.ClientError) and code not in RETRYABLE_CODES:
                    raise  # auth / bad request etc. — retrying won't help
                last_error = e
            except (httpx.TimeoutException, httpx.TransportError, RuntimeError) as e:
                last_error = e  # request hung / network blip / empty reply — retry
            if attempt < attempts_per_model:
                delay = min(base_delay * 2 ** (attempt - 1), 120) + random.uniform(0, 5)
                print(f"  -> {last_error}. Retrying in {delay:.0f}s")
                time.sleep(delay)
        print(f"{model} unavailable after {attempts_per_model} attempts, trying next model")
    raise RuntimeError(f"All models failed. Last error: {last_error}")

# 3. Generate Draft Post via Gemini
def generate_draft():
    folder_id = os.environ["DRIVE_FOLDER_ID"]
    context_notes = fetch_all_sources(folder_id)

    system_instruction = (
        "You are a Principal Product Designer and Design Systems Architect writing high-signal "
        "LinkedIn posts in the exact signature cadence of Zander Whitehurst.\n\n"
        "INPUT CONTEXT:\n"
        "You will be provided with field notes, architectural decisions, and case study documentation from Google Drive.\n"
        "Extract ONE real friction point, anti-pattern, system trade-off, or design decision from that context and translate it into Zander's short-cadence format.\n\n"
        "VOICE & CADENCE RULES:\n"
        "1. THE 2-LINE CONTRAST HOOK (Lines 1-2):\n"
        "   - Never use greetings, emojis, or throat-clearing.\n"
        "   - Start immediately with a sharp 2-line contrast or directive derived from the context.\n\n"
        "2. PACING & LAYOUT:\n"
        "   - Ultra-short lines. Write line-by-line like blank verse, separated by clean breaks.\n"
        "   - Do NOT write dense multi-sentence paragraphs.\n"
        "   - Use staccato lists to capture the real-world friction or micro-decisions.\n"
        "   - Use contrast: Addition vs. Subtraction, Speed vs. Clarity, Surface UI vs. System Logic.\n\n"
        "3. RESOLUTION & CRAFT:\n"
        "   - Re-anchor the problem back to the fundamentals: constraints, user clarity, cognitive load, or system simplicity.\n\n"
        "4. EXACT SIGN-OFF & TAGS:\n"
        "   - Conclude with the exact signature line:\n"
        "     Hope this perspective helps today ❤️\n"
        "   - Followed by a dash separator and clean lowercase hashtags:\n"
        "     —\n"
        "     #ux #ui #design #productdesign #designsystems #designengineer #ai #career\n\n"
        "5. IMAGE ONE-LINER (for the accompanying graphic):\n"
        "   - After the hashtags, write ONE punchy statement that distills the post's core idea, to be set as large type on a square image.\n"
        "   - Structure it as a 2-line contrast: line 1 sets up the claim, line 2 delivers the turn.\n"
        "     Example:\n"
        "       It's impossible to build a [design-led company]\n"
        "       Without [design-led leadership]\n"
        "   - Wrap the 1-2 key phrases to highlight in [square brackets] (one per line max).\n"
        "   - Max 14 words total. No emojis, no hashtags, no ending punctuation, no quotation marks.\n"
        "   - It must stand alone: someone who never reads the post should still get the idea.\n"
        "   - Do not simply copy the post's opening hook; sharpen it into the single most quotable line.\n\n"
        "OUTPUT FORMAT (STRICT):\n"
        "Output the raw LinkedIn post content ready to be published. Do not wrap in markdown code blocks or add any other meta-labels.\n"
        "Then, after the hashtags, add a blank line, a line containing exactly ===IMAGE===, and the 2-line image one-liner below it. Nothing after that."
    )

    # Hard 90s cap per request so a stalled call can't hang the job
    client = genai.Client(
        api_key=os.environ["GEMINI_API_KEY"],
        http_options=genai.types.HttpOptions(
            timeout=90_000,  # milliseconds
            retry_options=genai.types.HttpRetryOptions(attempts=1),  # we retry ourselves
        ),
    )

    prompt = f"Here is the background documentation and source notes:\n{context_notes}\n\nGenerate the LinkedIn post following all instructions."

    config = genai.types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.7,
    )

    # Primary model first, then fallbacks (override via GEMINI_MODELS="a,b,c")
    models = [m.strip() for m in os.environ.get(
        "GEMINI_MODELS", "gemini-3.8-flash,gemini-2.5-flash"
    ).split(",") if m.strip()]

    response = generate_with_retry(client, models, prompt, config)

    os.makedirs("drafts", exist_ok=True)
    with open("drafts/latest_post.md", "w", encoding="utf-8") as f:
        f.write(response.text.strip())

if __name__ == "__main__":
    generate_draft()
