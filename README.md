**# BlindEYE Server

Python/Flask backend for **BlindEYE**, an AI-powered assistive wearable for visually impaired users. The server receives a photo and a voice question from the headset, understands the scene, and sends back a spoken answer.

## How It Works

```
ESP32-CAM  ──(photo)──►  /upload/image ─┐
                                        ├─► session_id pairing
ESP32-S3   ──(audio)──►  /upload/audio ─┘
                                        │
                  faster-whisper (speech → text)
                                        │
                  Gemini (image + question → answer)
                                        │
                  gTTS (answer → speech)
                                        │
                  Cloudinary (audio hosting) ──► ESP32-S3 plays it
```

1. The user presses the button on the ESP32-CAM, which sends a photo.
2. The user presses the button on the ESP32-S3 and asks a question, which is sent as a WAV file.
3. The server pairs the image and the audio using a shared `session_id`.
4. The audio is transcribed locally with faster-whisper.
5. Gemini analyzes the image and answers the question.
6. gTTS turns the answer into speech, which is uploaded to Cloudinary.
7. The audio URL is returned to the device and played through the speaker.

## Hardware

| Board | Components | Role |
|---|---|---|
| ESP32-CAM | OV2640 camera, button | Captures and uploads photos |
| ESP32-S3 | INMP441 mic, MAX98357A amp, button (GPIO21 → GND) | Records the question, plays the answer |

The microphone and amplifier use separate I2S pins (no shared clocks).

## Tech Stack

- Python 3.10+
- Flask
- faster-whisper (local speech-to-text)
- Google Gemini (vision + language)
- gTTS (text-to-speech)
- Cloudinary (audio delivery)
- pydub + ffmpeg (audio processing)
- ngrok (public URL for the devices)

## Getting Started

### Prerequisites

- Python 3.10 or newer
- [ffmpeg](https://ffmpeg.org/download.html) installed and available in your `PATH`
- A Gemini API key
- A Cloudinary account
- An ngrok account

### Installation

```bash
git clone https://github.com/<your-username>/blindeye-server.git
cd blindeye-server
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### Environment Variables

Create a `.env` file in the project root:

| Variable | Description |
|---|---|
| `GEMINI_API_KEY` | Google Gemini API key |
| `CLOUDINARY_CLOUD_NAME` | Cloudinary cloud name |
| `CLOUDINARY_API_KEY` | Cloudinary API key |
| `CLOUDINARY_API_SECRET` | Cloudinary API secret |
| `PORT` | Server port (default `5000`) |

### Run

```bash
python server.py
```

In another terminal, expose the server so the ESP32 boards can reach it:

```bash
ngrok http 5000
```

Copy the ngrok HTTPS URL into the firmware of both ESP32 boards.

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/upload/image` | Receives a JPEG photo from the ESP32-CAM |
| `POST` | `/upload/audio` | Receives a WAV recording from the ESP32-S3 and returns the answer audio URL |

Both requests must include the same `session_id` (currently the fixed value `liveSession1`) so the server can pair the photo with the question.

## Troubleshooting

- **pydub cannot find ffmpeg**: make sure ffmpeg is installed and in your `PATH`, then restart the terminal.
- **Cloudinary errors**: check that `.env` is in the project root and the variables are loaded.
- **Devices cannot reach the server**: ngrok URLs change on each restart. Update the URL in the firmware.
- **Distorted or silent audio**: check the I2S bit-shifting and the sample rate in the ESP32-S3 firmware.

## Roadmap

- [x] Photo + voice pipeline (Whisper, Gemini, gTTS, Cloudinary)
- [ ] Object detection with distance estimation (YOLOv8)
- [ ] Alert system for nearby obstacles
- [ ] Dynamic `session_id` per device pair

## License

MIT**
