import json
import base64
import asyncio
import websockets
import os
import sys  # Added for inline progress dots
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse
from twilio.rest import Client

# Load sensitive values from environment variables
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "AC2db3da2359ec7eea8ec8e63bdf06de42")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "2111d9835709dae7531815fb57d15bbf")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "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")
REDIRECT_PHONE_NUMBER = os.getenv("REDIRECT_PHONE_NUMBER", "+18123207803")

PORT = int(os.getenv("PORT", 5050))

# Constants for AI Assistant Behavior
SYSTEM_MESSAGE = (
    "As soon as the call starts, say 'Hey, how can I help ya?' You are a helpful assistant "
    "who delivers very concise responses. Identify the customer's problem and redirect them to the right person "
    "according to their issue. If someone could use advice from a doctor, ask if they would like to be redirected. "
    "If they say yes, say 108. You must stop speaking if the user starts talking during your response."
)
VOICE = os.getenv("VOICE", "coral")
TEMPERATURE = float(os.getenv("TEMPERATURE", 1.1))

# Initialize Twilio client and FastAPI app
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
app = FastAPI()


# Root route
@app.get("/")
async def root():
    return {"message": "Welcome to the Heroku-deployed app!"}


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    global global_hostname
    global_hostname = request.url.hostname  # Save the hostname for later use in WebSocket
    response_content = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Connect>
            <Stream url="wss://{request.url.hostname}/media-stream" />
        </Connect>
    </Response>"""
    return HTMLResponse(content=response_content, media_type="application/xml")


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    global global_hostname
    await websocket.accept()

    try:
        async with websockets.connect(
            'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
            extra_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1"
            }
        ) as openai_ws:
            await send_session_update(openai_ws)

            stream_sid = None
            call_sid = None
            redirect_triggered = False
            ai_is_speaking = False

            async def receive_from_twilio():
                nonlocal stream_sid, call_sid, redirect_triggered, ai_is_speaking
                async for message in websocket.iter_text():
                    if redirect_triggered:
                        break
                    try:
                        data = json.loads(message)
                        if data['event'] == 'start':
                            stream_sid = data['start']['streamSid']
                            call_sid = data['start'].get('callSid')
                        elif data['event'] == 'media' and openai_ws.open:
                            if ai_is_speaking:
                                await send_stop_audio(openai_ws)
                                ai_is_speaking = False
                            audio_append = {
                                "type": "input_audio_buffer.append",
                                "audio": data['media']['payload']
                            }
                            await openai_ws.send(json.dumps(audio_append))
                            sys.stdout.write(".")  # Print a dot for activity
                            sys.stdout.flush()  # Ensure immediate output
                        elif data['event'] == 'stop':
                            break
                    except Exception as e:
                        print(f"ERROR: Error processing Twilio message: {e}")

            async def process_openai_responses():
                nonlocal redirect_triggered, ai_is_speaking
                async for openai_message in openai_ws:
                    if redirect_triggered:
                        break
                    try:
                        response = json.loads(openai_message)
                        if response['type'] == 'response.audio_transcript.delta' and response.get('delta'):
                            transcribed_text = response['delta']
                            print(f"\nAI TRANSCRIPT| {transcribed_text}")  # Log user transcription
                            keyword = "108"
                            if keyword in transcribed_text.lower():
                                redirect_triggered = True
                                if call_sid:
                                    twilio_client.calls(call_sid).update(
                                        url=f"https://{global_hostname}/redirecting-call",
                                        method="POST"
                                    )
                                break
                        elif response['type'] == 'response.audio.delta' and response.get('delta'):
                            ai_is_speaking = True
                            try:
                                audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                                audio_delta = {
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {
                                        "payload": audio_payload
                                    }
                                }
                                await websocket.send_json(audio_delta)
                            except Exception as e:
                                print(f"ERROR: Failed to process audio delta: {e}")
                    except Exception as e:
                        print(f"ERROR: Error processing OpenAI WebSocket message: {e}")

            try:
                await asyncio.gather(receive_from_twilio(), process_openai_responses())
            except Exception as e:
                print(f"ERROR: Error in WebSocket handling: {e}")
    except Exception as e:
        print(f"ERROR: Error connecting to OpenAI WebSocket: {e}")
    finally:
        await websocket.close()


@app.api_route("/redirecting-call", methods=["POST"])
async def handle_redirecting_call(request: Request):
    response_content = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Dial>{REDIRECT_PHONE_NUMBER}</Dial>
    </Response>"""
    return HTMLResponse(content=response_content, media_type="application/xml")


async def send_session_update(openai_ws):
    try:
        session_update = {
            "type": "session.update",
            "session": {
                "turn_detection": {"type": "server_vad"},
                "input_audio_format": "g711_ulaw",
                "output_audio_format": "g711_ulaw",
                "voice": VOICE,
                "instructions": SYSTEM_MESSAGE,
                "modalities": ["text", "audio"],
                "temperature": TEMPERATURE,
                "input_audio_transcription": {
                    "model": "whisper-1"
                }
            }
        }
        await openai_ws.send(json.dumps(session_update))
    except Exception as e:
        print(f"ERROR: Failed to send session update to OpenAI: {e}")


async def send_stop_audio(openai_ws):
    try:
        stop_audio = {"type": "stop_audio_output"}
        await openai_ws.send(json.dumps(stop_audio))
    except Exception as e:
        print(f"ERROR: Failed to send stop_audio_output command: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
