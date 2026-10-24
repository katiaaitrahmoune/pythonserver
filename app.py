"""
BlindEye single-file backend.
Replaces the previous Node (api) + Node/ngrok (worker) + Python subprocess setup
with one Python process. No Docker, no ngrok, no subprocess calls.

Flow (same session logic as your original Node code):
  1. ESP32 uploads image -> POST /upload/image?session_id=XXX
  2. ESP32 uploads audio -> POST /upload/audio?session_id=XXX
  3. Whichever arrives second triggers processing:
       - faster-whisper transcribes the audio (local, free)
       - Gemini describes the image given the transcribed question (free tier)
       - gTTS converts Gemini's text answer to speech (free)
       - ffmpeg (via pydub) converts mp3 -> wav (mono, 16kHz, matches ESP32 code)
       - Cloudinary hosts the wav and returns a URL
  4. Both endpoints return { "url": "<cloudinary wav url>" }

Requirements: see requirements.txt
You still need ffmpeg installed and on PATH (pydub/gTTS conversion needs it) --
this is much lighter than the previous Docker+ngrok+Node worker setup.
"""

import os
import time
import threading
import tempfile

from flask import Flask, request, jsonify
from dotenv import load_dotenv

from faster_whisper import WhisperModel
import google.generativeai as genai
from gtts import gTTS
from pydub import AudioSegment

# Bypass Windows PATH issues entirely -- point pydub directly at the
# ffmpeg/ffprobe executables. Update this path if yours differs.

import cloudinary
import cloudinary.uploader

load_dotenv()

app = Flask(__name__)

# ---- Config ----
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY    = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")
GEMINI_API_KEY         = os.getenv("API_GEMINI")
SESSION_TTL_SECONDS    = 60

cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET,
)

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel(
    model_name="gemini-3.5-flash",
    system_instruction=(
        "Your name is BlindEye. You help visually impaired users by describing "
        "images clearly and concisely. Always start with \"Hi I'm BlindEye\". "
        "Keep your response under 200 characters. Use simple language, only "
        "describe visible objects."
    ),
)

# faster-whisper: "tiny" model, int8 quantization for speed on CPU
whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")

# ---- In-memory session store (mirrors your original Node logic) ----
sessions = {}
sessions_lock = threading.Lock()


class Session:
    def __init__(self):
        self.audio_path = None
        self.image_path = None
        self.result_url = None
        self.event = threading.Event()
        self.timer = threading.Timer(SESSION_TTL_SECONDS, self._timeout)

    def _timeout(self):
        self.event.set()  # wake up any waiter with result_url still None


def get_or_create_session(session_id):
    with sessions_lock:
        if session_id not in sessions:
            s = Session()
            s.timer.start()
            sessions[session_id] = s
        return sessions[session_id]


def cleanup_session(session_id):
    with sessions_lock:
        s = sessions.pop(session_id, None)
    if s:
        s.timer.cancel()
        for p in (s.audio_path, s.image_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


# ---- Processing pipeline ----
def transcribe_audio(audio_path):
    segments, _ = whisper_model.transcribe(audio_path)
    text = " ".join(seg.text for seg in segments).strip()
    return text


def describe_image(question_text, image_path):
    with open(image_path, "rb") as f:
        image_bytes = f.read()

    result = gemini_model.generate_content([
        question_text,
        {"mime_type": "image/jpeg", "data": image_bytes},
    ])

    text = result.text.strip()
    if not text.startswith("Hi I'm BlindEye"):
        text = "Hi I'm BlindEye. " + text
    if len(text) > 200:
        text = text[:197] + "..."
    return text


def text_to_wav(text):
    tmp_dir = tempfile.gettempdir()
    mp3_path = os.path.join(tmp_dir, f"tts-{int(time.time()*1000)}.mp3")
    wav_path = os.path.join(tmp_dir, f"tts-{int(time.time()*1000)}.wav")

    tts = gTTS(text=text, lang="en")
    tts.save(mp3_path)

    audio = AudioSegment.from_mp3(mp3_path)
    audio = audio.set_channels(1).set_frame_rate(16000)
    audio.export(wav_path, format="wav")

    os.remove(mp3_path)
    return wav_path


def upload_wav_to_cloudinary(wav_path):
    result = cloudinary.uploader.upload(
        wav_path,
        resource_type="video",  # Cloudinary treats audio under "video" resource type
        folder="blindeye/tts",
        format="wav",
        overwrite=False,
    )
    return result["secure_url"]


def process_session(session):
    try:
        question_text = transcribe_audio(session.audio_path)
        print("Whisper done:", question_text)

        answer_text = describe_image(question_text, session.image_path)
        print("Gemini done:", answer_text)

        wav_path = text_to_wav(answer_text)
        print("TTS done:", wav_path)

        url = upload_wav_to_cloudinary(wav_path)
        print("Cloudinary upload done:", url)

        os.remove(wav_path)
        return url
    except Exception as e:
        print("Processing error:", e)
        return None


# ---- Routes ----
@app.route("/upload/image", methods=["POST"])
def upload_image():
    session_id = request.args.get("session_id")
    if not session_id:
        return jsonify({"error": "missing session_id"}), 400

    file = request.files.get("image")
    if not file:
        return jsonify({"error": "missing image file"}), 400

    session = get_or_create_session(session_id)
    tmp_path = os.path.join(tempfile.gettempdir(), f"img-{session_id}.jpg")
    file.save(tmp_path)
    session.image_path = tmp_path

    if session.audio_path:
        # Audio already here -> process now, then wake up the waiting audio request
        url = process_session(session)
        session.result_url = url
        session.event.set()
        return jsonify({"url": url}) if url else (jsonify({"error": "processing failed"}), 500)

    return jsonify({"status": "waiting_for_audio"}), 202


@app.route("/upload/audio", methods=["POST"])
def upload_audio():
    session_id = request.args.get("session_id")
    if not session_id:
        return jsonify({"error": "missing session_id"}), 400

    file = request.files.get("audio")
    if not file:
        return jsonify({"error": "missing audio file"}), 400

    session = get_or_create_session(session_id)
    tmp_path = os.path.join(tempfile.gettempdir(), f"aud-{session_id}.wav")
    file.save(tmp_path)
    session.audio_path = tmp_path

    if session.image_path:
        # Image already here -> process immediately
        url = process_session(session)
        cleanup_session(session_id)
        return jsonify({"url": url}) if url else (jsonify({"error": "processing failed"}), 500)

    # Wait for the image to arrive (mirrors the Node Promise-based wait)
    got_signal = session.event.wait(timeout=SESSION_TTL_SECONDS)
    url = session.result_url
    cleanup_session(session_id)

    if not got_signal or not url:
        return jsonify({"error": "timeout waiting for image"}), 408

    return jsonify({"url": url})


@app.route("/", methods=["GET"])
def health():
    return "BlindEye API running (single-file Python)"


if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port, threaded=True)