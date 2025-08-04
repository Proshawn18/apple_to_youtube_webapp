import os
import json
import uuid

import google_auth_oauthlib.flow
import googleapiclient.discovery
import requests
from bs4 import BeautifulSoup
from flask import Flask, redirect, render_template, request, session, url_for
from google.oauth2.credentials import Credentials

# --- Flask App Configuration ---
app = Flask(__name__)
# The SECRET_KEY is used to sign the session cookie.
# In production, set this as an environment variable for security.
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "super-secret-key-for-dev")
# This allows OAuth to run on http:// for local testing.
# This is safe in Cloud Run because Google's proxy handles the TLS termination.
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# --- Google API Configuration ---
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
API_SERVICE_NAME = "youtube"
API_VERSION = "v3"


# --- Helper Functions ---

def get_client_config():
    """Loads client config from the GOOGLE_CLIENT_SECRET_JSON environment variable."""
    config_str = os.environ.get("GOOGLE_CLIENT_SECRET_JSON")
    if not config_str:
        raise ValueError("Missing GOOGLE_CLIENT_SECRET_JSON environment variable. Please set it to the content of your client_secret.json file.")
    return json.loads(config_str)

def scrape_apple_music_playlist(url):
    """Fetches an Apple Music playlist and scrapes track titles and artists."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        script_tag = soup.find('script', id='serialized-server-data')
        if not script_tag:
            return None, "Could not find playlist data on the page."

        data = json.loads(script_tag.string)
        playlist_name = data[0]['data']['name']

        track_list_items = []
        for section in data[0]['data']['sections']:
            if section.get('itemKind') == 'trackLockup':
                track_list_items.extend(section.get('items', []))

        tracks = []
        for track in track_list_items:
            title = track.get('title', 'Unknown Title')
            artist = track.get('artistName', 'Unknown Artist')
            tracks.append(f"{title} by {artist}")

        return playlist_name, tracks
    except requests.exceptions.RequestException as e:
        return None, f"Network error fetching Apple Music URL: {e}"
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        return None, f"Error parsing playlist data: {e}"
    except Exception as e:
        return None, f"An unknown error occurred during scraping: {e}"


def credentials_to_dict(credentials):
    """Helper function to serialize credentials for the session."""
    return {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }


# --- Flask Routes ---

@app.route('/')
def index():
    """Renders the main page with the input form."""
    return render_template('index.html')


@app.route('/migrate', methods=['POST'])
def migrate():
    """Initiates the migration process by starting the OAuth flow."""
    apple_music_url = request.form.get('apple_music_url')
    if not apple_music_url:
        return "Please provide an Apple Music playlist URL.", 400

    session['apple_music_url'] = apple_music_url

    client_config = get_client_config()
    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        client_config, scopes=SCOPES
    )

    flow.redirect_uri = url_for('oauth2callback', _external=True)

    authorization_url, state = flow.authorization_url(
        access_type='offline', include_granted_scopes='true'
    )
    session['state'] = state

    return redirect(authorization_url)


@app.route('/oauth2callback')
def oauth2callback():
    """Callback route for Google OAuth. Exchanges the authorization code for credentials."""
    state = session['state']
    
    client_config = get_client_config()
    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        client_config, scopes=SCOPES, state=state
    )
    flow.redirect_uri = url_for('oauth2callback', _external=True)

    authorization_response = request.url
    flow.fetch_token(authorization_response=authorization_response)

    credentials = flow.credentials
    session['credentials'] = credentials_to_dict(credentials)

    return redirect(url_for('process_playlist'))


@app.route('/process')
def process_playlist():
    """The core logic: scrapes tracks, creates a YouTube playlist, and adds videos."""
    if 'credentials' not in session or 'apple_music_url' not in session:
        return redirect(url_for('index'))

    credentials = Credentials(**session['credentials'])
    youtube = googleapiclient.discovery.build(
        API_SERVICE_NAME, API_VERSION, credentials=credentials
    )

    apple_music_url = session.pop('apple_music_url', None)

    playlist_name, tracks = scrape_apple_music_playlist(apple_music_url)
    if not tracks:
        return render_template('results.html', error=playlist_name or "Could not scrape playlist.")

    try:
        playlist_title = f"{playlist_name} (Migrated from Apple Music)"
        playlist_request = youtube.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": playlist_title,
                    "description": f"Playlist migrated from {apple_music_url}"
                },
                "status": {"privacyStatus": "private"}
            }
        )
        playlist_response = playlist_request.execute()
        playlist_id = playlist_response["id"]
        playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
    except googleapiclient.errors.HttpError as e:
        return render_template('results.html', error=f"Could not create YouTube playlist: {e}")

    migrated_count = 0
    errors = []
    for track_query in tracks:
        try:
            search_request = Youtube().list(
                part="snippet", q=track_query, type="video", maxResults=1
            )
            search_response = search_request.execute()

            if search_response["items"]:
                video_id = search_response["items"][0]["id"]["videoId"]

                youtube.playlistItems().insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "playlistId": playlist_id,
                            "resourceId": {"kind": "youtube#video", "videoId": video_id}
                        }
                    }
                ).execute()
                migrated_count += 1
        except googleapiclient.errors.HttpError as e:
            errors.append(f"Could not add '{track_query}': {e}")

    return render_template(
        'results.html',
        playlist_url=playlist_url,
        total_songs=len(tracks),
        migrated_count=migrated_count,
        errors=errors
    )


if __name__ == '__main__':
    # For local development, this will run on port 8080.
    # For Cloud Run, the PORT environment variable is used.
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=True)