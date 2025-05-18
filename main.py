from flask import Flask, request, jsonify, send_from_directory
import os, requests, uuid, subprocess
import mimetypes
from openai import OpenAI

from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)
os.makedirs("temp", exist_ok=True)

@app.route("/health")
def health():
    return "OK", 200

@app.route("/")
def index():
    return jsonify({"message": "FFmpeg + Whisper API is live!"})

def download_video(url, out_path):
    r = requests.get(url, stream=True)
    with open(out_path, 'wb') as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)

def transcribe_audio(audio_path):
    with open(audio_path, "rb") as f:
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="text"
        )
    return response

    
def get_broll_timestamp(transcript_text):
    prompt = f"""
You are helping edit a video. Based on the transcript below, suggest one time (in seconds, less than 30) where a B-roll clip could be inserted to improve the viewer's experience.

Transcript:
\"\"\"{transcript_text}\"\"\"

Reply with just the number of seconds (an integer, less than 30).
"""
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.5
    )
    content = response.choices[0].message.content.strip()
    try:
        return int(content)
    except:
        return 5  # fallback if GPT gets creative

@app.route("/auto-splice", methods=["POST"])
def auto_splice():
    data = request.get_json()
    main_url = data.get("main_video_url")
    brolls = data.get("broll_clips", [])
    broll = brolls[0]

    video_id = str(uuid.uuid4())
    main_path = f"temp/{video_id}_main.mp4"
    broll_path = f"temp/{video_id}_broll.mp4"
    output_path = f"temp/{video_id}_output.mp4"
    pre_path = f"temp/{video_id}_pre.mp4"
    post_path = f"temp/{video_id}_post.mp4"
    list_path = f"temp/{video_id}_list.txt"

    # Step 1: Download the main video and B-roll
    download_video(main_url, main_path)
    download_video(broll["url"], broll_path)

    # ✅ Step 2: Normalize both videos to ensure matching codecs/resolution
    norm_main = f"temp/{video_id}_main_norm.mp4"
    norm_broll = f"temp/{video_id}_broll_norm.mp4"

    subprocess.run(f"ffmpeg -y -i {main_path} -vf scale=1280:720 -r 30 -c:v libx264 -c:a aac -strict experimental {norm_main}", shell=True)
    subprocess.run(f"ffmpeg -y -i {broll_path} -vf scale=1280:720 -r 30 -c:v libx264 -c:a aac -strict experimental {norm_broll}", shell=True)

    # Step 3: Extract audio from normalized main video
    audio_path = f"temp/{video_id}_audio.mp3"
    subprocess.run(f"ffmpeg -y -i {norm_main} -vn -acodec libmp3lame -ar 44100 -ac 2 -b:a 128k {audio_path}", shell=True)

    # Step 4: Transcribe the audio
    transcript = transcribe_audio(audio_path)

    # Step 5: Use GPT to pick splice time
    splice_time = get_broll_timestamp(transcript)

    # ✅ Step 6: Split normalized main video
    subprocess.run(f"ffmpeg -y -i {norm_main} -t {splice_time} -c copy {pre_path}", shell=True)
    subprocess.run(f"ffmpeg -y -i {norm_main} -ss {splice_time} -c copy {post_path}", shell=True)

    # Step 7: Create concat list with normalized clips
    with open(list_path, "w") as f:
        f.write(f"file '{os.path.basename(pre_path)}'\n")
        f.write(f"file '{os.path.basename(norm_broll)}'\n")
        f.write(f"file '{os.path.basename(post_path)}'\n")

    # ✅ Step 8: Concatenate everything into the final video
    subprocess.run(
        f"ffmpeg -y -f concat -safe 0 -i {list_path} -c:v libx264 -c:a aac {output_path}",
        shell=True
    )

    # Optional: trim final output to 30s for faster testing
    import time
    time.sleep(2)

    trimmed_path = f"temp/trimmed_{video_id}.mp4"
    if os.path.exists(output_path):
        subprocess.run(
            f"ffmpeg -y -i {output_path} -t 30 -c copy {trimmed_path}",
            shell=True
        )
        output_path = trimmed_path

    return jsonify({
        "status": "complete",
        "timestamp_used": splice_time,
        "transcript": transcript,
        "output": f"/{output_path}"
    })

@app.route('/temp/<path:filename>')
def download_file(filename):
    return send_from_directory('temp', filename)
    
@app.route("/overlay-broll", methods=["POST"])
def overlay_broll():
    data = request.get_json()
    main_url = data.get("main_video_url")
    broll_url = data.get("broll_clips", [])[0]["url"]

    video_id = str(uuid.uuid4())
    main_path = f"temp/{video_id}_main.mp4"
    broll_path = f"temp/{video_id}_broll.mp4"
    norm_main = f"temp/{video_id}_main_norm.mp4"
    norm_broll = f"temp/{video_id}_broll_norm.mp4"
    trimmed_broll = f"temp/{video_id}_broll_trimmed.mp4"
    audio_path = f"temp/{video_id}_audio.mp3"
    output_path = f"temp/{video_id}_overlay_output.mp4"

    # Step 1: Download videos
    download_video(main_url, main_path)
    download_video(broll_url, broll_path)

    # Step 2: Normalize both videos (lower resolution and FPS to reduce memory)
    subprocess.run(f"ffmpeg -y -i {main_path} -vf scale=640:360 -r 24 -c:v libx264 -c:a aac {norm_main}", shell=True)


    # Step 3: Extract audio for transcription
    subprocess.run(f"ffmpeg -y -i {norm_main} -vn -acodec libmp3lame {audio_path}", shell=True)

    # Step 4: Transcribe + get GPT timestamp (safely clamped < 30)
    transcript = transcribe_audio(audio_path)
    timestamp = 5
    if timestamp > 30:
        timestamp = 5  # fallback to safe value
    broll_duration = 5

    # Step 5: Trim and normalize B-roll in one pass
    subprocess.run(
        f"ffmpeg -y -i {broll_path} -t {broll_duration} -vf scale=640:360,setpts=PTS-STARTPTS -r 24 -c:v libx264 -preset veryfast {trimmed_broll}",
        shell=True
    )


    # Step 6: Overlay B-roll visually with main audio uninterrupted
    overlay_filter = f"[0:v][1:v] overlay=enable='between(t,{timestamp},{timestamp + 5})':eof_action=stop,format=auto"
    subprocess.run(
        f"ffmpeg -y -i {norm_main} -i {trimmed_broll} -filter_complex \"{overlay_filter}\" -map 0:a -c:v libx264 -c:a aac {output_path}",
        shell=True
    )

    return jsonify({
        "status": "overlay complete",
        "timestamp_used": timestamp,
        "broll_duration": broll_duration,
        "output": f"/{output_path}"
    })

@app.route("/process-chunk", methods=["POST"])
def process_chunk():
    data = request.get_json()
    main_url = data.get("main_video_url")
    broll_url = data.get("broll_url")
    start_time = int(data.get("start_time", 0))  # in seconds
    duration = 30  # max chunk duration

    video_id = str(uuid.uuid4())
    raw_main = f"temp/{video_id}_raw.mp4"
    chunk_main = f"temp/{video_id}_chunk.mp4"
    norm_main = f"temp/{video_id}_norm.mp4"
    broll_path = f"temp/{video_id}_broll.mp4"
    trimmed_broll = f"temp/{video_id}_broll_trimmed.mp4"
    audio_path = f"temp/{video_id}_audio.mp3"
    output_path = f"temp/{video_id}_output.mp4"

    # Step 1: Download full main video
    download_video(main_url, raw_main)

    # Step 2: Extract 30s chunk
    subprocess.run(
        f"ffmpeg -y -ss {start_time} -t {duration} -i {raw_main} -c copy {chunk_main}",
        shell=True
    )

    # Step 3: Normalize chunk
    subprocess.run(
        f"ffmpeg -y -i {chunk_main} -vf scale=480:270 -r 20 -c:v libx264 -c:a aac {norm_main}",
        shell=True
    )

    # Step 4: Download B-roll and trim
    download_video(broll_url, broll_path)
    subprocess.run(
        f"ffmpeg -y -i {broll_path} -t 5 -vf scale=480:270,setpts=PTS-STARTPTS -r 20 -c:v libx264 -preset veryfast {trimmed_broll}",
        shell=True
    )

    # Step 5: Transcribe audio + get timestamp from GPT
    subprocess.run(f"ffmpeg -y -i {norm_main} -vn -acodec libmp3lame {audio_path}", shell=True)
    transcript = transcribe_audio(audio_path)
    timestamp = 5  # Force overlay to appear at 5 seconds
    print(f"[DEBUG] Hardcoded B-roll timestamp to 5s for test.")
    if timestamp < 0 or timestamp > (duration - 5):
        print(f"[WARN] GPT gave out-of-bounds timestamp: {timestamp}. Clamping to 0.")
        timestamp = 0

    # Step 6: Overlay B-roll at GPT-selected time
    overlay_filter = f"[0:v][1:v] overlay=enable='between(t,{timestamp},{timestamp + 5})':eof_action=stop,format=auto"
    subprocess.run(f"ffmpeg -y -i {norm_main} -i {trimmed_broll} -filter_complex \"{overlay_filter}\" -map 0:a -shortest -c:v libx264 -c:a aac {output_path}",
    shell=True
)

    return jsonify({
        "status": "processed",
        "chunk_start": start_time,
        "broll_timestamp": timestamp,
        "output": f"/{output_path}"
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

