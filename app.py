import os
import json
import uuid
import requests
from bs4 import BeautifulSoup
from flask import Flask, redirect, render_template, request, session, url_for
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- Flask App Configuration ---
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "super-secret-key-for-dev")
# Allow OAuth on http://localhost for testing
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# --- Google API Configuration ---
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
API_SERVICE_NAME = "youtube"
API_VERSION = "v3"

# --- Helper Functions ---

def get_client_config():
    """Load client config JSON from an environment variable."""
    config_str = os.environ.get("GOOGLE_CLIENT_SECRET_JSON")
    if not config_str:
        raise ValueError(
            "Missing GOOGLE_CLIENT_SECRET_JSON; set it to your client_secret.json contents."
        )
    return json.loads(config_str)

def credentials_to_dict(creds: Credentials) -> dict:
    """Serialize OAuth2 credentials to store in the Flask session."""
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }

def scrape_apple_music_playlist(url: str):
    """Fetch an Apple Music playlist and extract track titles + artists."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # look for the serialized data script (may change over time)
        script = soup.find("script", id="serialized-server-data") or soup.find("script", {"type": "application/json"})
        if not script or not script.string:
            return None, "Could not locate embedded playlist data."

        data = json.loads(script.string)
        # adjust navigation to actual JSON shape if needed
        playlist_info = data[0]["data"]
        playlist_name = playlist_info.get("name", "Apple Music Playlist")
        items = []
        for section in playlist_info.get("sections", []):
            if section.get("itemKind") == "trackLockup":
                items.extend(section.get("items", []))

        tracks = [
            f"{t.get('title','Unknown Title')} by {t.get('artistName','Unknown Artist')}"
            for t in items
        ]
        return playlist_name, tracks

    except requests.RequestException as e:
        return None, f"Network error: {e}"
    except (ValueError, KeyError, IndexError, TypeError) as e:
        return None, f"Parsing error: {e}"

# --- Flask Routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/migrate", methods=["POST"])
def migrate():
    apple_url = request.form.get("apple_music_url")
    if not apple_url:
        return "Please provide an Apple Music playlist URL.", 400

    session["apple_music_url"] = apple_url
    client_config = get_client_config()
    flow = Flow.from_client_config(client_config, SCOPES)
    flow.redirect_uri = url_for("oauth2callback", _external=True)

    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true"
    )
    session["state"] = state
    return redirect(auth_url)


@app.route("/oauth2callback")
def oauth2callback():
    state = session.get("state")
    if not state:
        return redirect(url_for("index"))

    client_config = get_client_config()
    flow = Flow.from_client_config(client_config, SCOPES, state=state)
    flow.redirect_uri = url_for("oauth2callback", _external=True)

    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    session["credentials"] = credentials_to_dict(creds)
    return redirect(url_for("process_playlist"))


@app.route("/process")
def process_playlist():
    if "credentials" not in session or "apple_music_url" not in session:
        return redirect(url_for("index"))

    # reconstruct credentials
    creds = Credentials(**session["credentials"])
    # refresh if expired
    if creds.expired and creds.refresh_token:
        creds.refresh(requests.Request())
        session["credentials"] = credentials_to_dict(creds)

    youtube = build(API_SERVICE_NAME, API_VERSION, credentials=creds)
    apple_url = session.pop("apple_music_url", None)
    playlist_name, tracks_or_error = scrape_apple_music_playlist(apple_url)

    if not playlist_name or not isinstance(tracks_or_error, list):
        return render_template(
            "results.html", error=tracks_or_error or "Failed to scrape playlist."
        )

    tracks = tracks_or_error
    try:
        # create the YouTube playlist
        body = {
            "snippet": {
                "title": f"{playlist_name} (migrated)",
                "description": f"Migrated from Apple Music: {apple_url}"
            },
            "status": {"privacyStatus": "private"}
        }
        playlist_resp = youtube.playlists().insert(part="snippet,status", body=body).execute()
        playlist_id = playlist_resp["id"]
        playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
    except HttpError as e:
        return render_template("results.html", error=f"Playlist creation error: {e}")

    migrated, errors = 0, []
    for q in tracks:
        try:
            search_resp = youtube.search().list(
                part="snippet", q=q, type="video", maxResults=1
            ).execute()

            items = search_resp.get("items", [])
            if items:
                vid = items[0]["id"]["videoId"]
                youtube.playlistItems().insert(
                    part="snippet",
                    body={
                        "snippet": {"playlistId": playlist_id,
                                    "resourceId": {"kind": "youtube#video", "videoId": vid}}
                    }
                ).execute()
                migrated += 1
        except HttpError as e:
            errors.append(f"Failed to add '{q}': {e}")

    return render_template(
        "results.html",
        playlist_url=playlist_url,
        total_songs=len(tracks),
        migrated_count=migrated,
        errors=errors,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
