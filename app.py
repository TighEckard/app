import json
import base64
import asyncio
import websockets
import os
import sys  # Added for inline progress dots
import time  # For performance logging
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from twilio.rest import Client



TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
REDIRECT_PHONE_NUMBER = os.getenv("REDIRECT_PHONE_NUMBER")

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
@app.api_route("/", methods=["GET", "HEAD"])
async def root(request: Request):
    if request.method == "HEAD":
        return HTMLResponse(content="", status_code=200)
    return JSONResponse(content={"message": "Welcome to the Heroku-deployed app!"})

@app.api_route("/incoming-call", methods=["GET", "POST", "HEAD"])
async def handle_incoming_call(request: Request):
    if request.method == "HEAD":
        return HTMLResponse(content="", status_code=200)
    
    start_time = time.time()
    try:
        global global_hostname
        global_hostname = request.url.hostname
        print(f"Received incoming call request. Hostname: {global_hostname}")
        
        response_content = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Connect>
                <Stream url="wss://{request.url.hostname}/media-stream" />
            </Connect>
        </Response>"""
        print("Returning Twilio XML response.")
        return HTMLResponse(content=response_content, media_type="application/xml")
    except Exception as e:
        print(f"ERROR in /incoming-call: {e}")
        return HTMLResponse(content="<Response><Reject/></Response>", media_type="application/xml", status_code=500)
    finally:
        print(f"/incoming-call processed in {time.time() - start_time:.2f} seconds")

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    try:
        print("Attempting to establish WebSocket connection.")
        await websocket.accept()
        print("WebSocket connection accepted.")
        
        async with websockets.connect(
            'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
            extra_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1"
            }
        ) as openai_ws:
            print("Connected to OpenAI WebSocket.")
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
                            print(f"Stream SID: {stream_sid}, Call SID: {call_sid}")
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
                            print("Twilio stream stopped.")
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
                                    print("Redirecting call to the specified number.")
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
        print("Closing WebSocket connection.")
        await websocket.close()

@app.api_route("/redirecting-call", methods=["POST"])
async def handle_redirecting_call(request: Request):
    try:
        response_content = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Dial>{REDIRECT_PHONE_NUMBER}</Dial>
        </Response>"""
        print("Redirecting call.")
        return HTMLResponse(content=response_content, media_type="application/xml")
    except Exception as e:
        print(f"ERROR in /redirecting-call: {e}")
        return HTMLResponse(content="<Response><Reject/></Response>", media_type="application/xml", status_code=500)

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
        print("Session update sent to OpenAI.")
    except Exception as e:
        print(f"ERROR: Failed to send session update to OpenAI: {e}")

async def send_stop_audio(openai_ws):
    try:
        stop_audio = {"type": "stop_audio_output"}
        await openai_ws.send(json.dumps(stop_audio))
        print("Stop audio command sent to OpenAI.")
    except Exception as e:
        print(f"ERROR: Failed to send stop_audio_output command: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
