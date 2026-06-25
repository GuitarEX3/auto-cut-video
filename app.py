#!/usr/bin/env python3
"""
Video Editor Web App - Flask backend
Run: python app.py  then open http://localhost:5000
"""
import os, json, re, textwrap, threading, uuid, asyncio
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder="static")

# ── Storage: ใช้ /data บน Render (persistent disk), /tmp เป็น fallback ──────
def _resolve_base() -> Path:
    disk = os.environ.get("RENDER_DISK_PATH", "")
    if disk:
        p = Path(disk)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except (PermissionError, OSError) as e:
            print(f"WARNING: cannot use {disk} ({e}), falling back to /tmp")
            return Path("/tmp")
    return Path(".")

_BASE = _resolve_base()
UPLOAD_DIR = _BASE / "uploads"
OUTPUT_DIR = _BASE / "outputs"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

jobs = {}

THAI_VOICES = {
    "th-TH-PremwadeeNeural": "ผู้หญิง – เปรมวดี (ทางการ)",
    "th-TH-NiwatNeural":     "ผู้ชาย – นิวัฒน์ (ทางการ)",
    "th-TH-AcharaNeural":    "ผู้หญิง – อาชรา (เป็นกันเอง)",
}


# ── TTS ───────────────────────────────────────────────────────────────────────
async def _edge_tts_async(text: str, voice: str, out_path: str):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_path)


def tts_edge(text: str, voice: str, out_path: str) -> bool:
    try:
        asyncio.run(_edge_tts_async(text, voice, out_path))
        return True
    except Exception as e:
        print(f"Edge TTS error: {e}")
        return False


def tts_gtts(text: str, out_path: str):
    from gtts import gTTS
    gTTS(text=text, lang="th", slow=False).save(out_path)


# ── AI scene splitter ─────────────────────────────────────────────────────────
def split_script_ai(script_text: str, duration: float, api_key: str = "") -> list:
    if api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            prompt = f"""Split this script into scenes for a {duration:.1f}-second video.
Script:
\"\"\"{script_text}\"\"\"
Rules: 3-8 scenes, total time = {duration:.1f}s exactly, distribute by text length.
Return ONLY valid JSON array:
[{{"scene":1,"start":0.0,"end":5.0,"text":"..."}}]"""
            msg = client.messages.create(
                model="claude-sonnet-4-6", max_tokens=1024,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = re.sub(r"```json|```", "", msg.content[0].text).strip()
            return json.loads(raw)
        except Exception as e:
            print(f"AI split failed: {e}, using auto-split")

    sents = re.split(r'(?<=[.!?。])\s+', script_text)
    sents = [s.strip() for s in sents if s.strip()]
    if not sents:
        sents = textwrap.wrap(script_text, 80)
    total = sum(len(s) for s in sents)
    scenes, cur = [], 0.0
    for i, s in enumerate(sents):
        dur = duration * len(s) / total
        scenes.append({"scene": i + 1, "start": round(cur, 2), "end": round(cur + dur, 2), "text": s})
        cur += dur
    if scenes:
        scenes[-1]["end"] = round(duration, 2)
    return scenes


def make_sub_clip(text, clip_w, clip_duration):
    try:
        from moviepy.editor import TextClip
        wrapped = "\n".join(textwrap.wrap(text, 38))
        return (TextClip(wrapped, fontsize=26, color="white",
                         stroke_color="black", stroke_width=1.5,
                         method="caption", size=(clip_w - 40, None))
                .set_position(("center", "bottom"))
                .set_duration(clip_duration)
                .margin(bottom=18, opacity=0))
    except Exception as e:
        print(f"Subtitle error: {e}")
        return None


# ── Video build job ───────────────────────────────────────────────────────────
def build_job(job_id, video_path, script_text, use_tts, use_subs, tts_voice, api_key, audio_path=None):
    def update(status, progress, message, output=None):
        jobs[job_id].update({"status": status, "progress": progress, "message": message})
        if output:
            jobs[job_id]["output"] = output

    open_clips = []
    tts_paths  = []

    try:
        from moviepy.editor import (
            VideoFileClip, concatenate_videoclips,
            CompositeVideoClip, AudioFileClip
        )

        update("running", 5, "กำลังอ่านวิดีโอ...")
        src = VideoFileClip(video_path)
        open_clips.append(src)
        duration = src.duration

        if audio_path and not script_text.strip():
            script_text = "."

        update("running", 15, "AI กำลังแบ่ง scene...")
        scenes = split_script_ai(script_text, duration, api_key)
        if not scenes:
            raise ValueError("ไม่สามารถแบ่ง scene ได้ กรุณาตรวจสอบสคริป")

        # ── สร้าง / ตัดเสียงต่อ scene ────────────────────────────────────────
        if audio_path:
            update("running", 30, f"กำลังตัดเสียงจากไฟล์ตาม {len(scenes)} scene...")
            src_audio = AudioFileClip(audio_path)
            open_clips.append(src_audio)
            for sc in scenes:
                p = str(OUTPUT_DIR / f"{job_id}_tts_{sc['scene']}.mp3")
                s = min(sc["start"], src_audio.duration - 0.01)
                e = min(sc["end"],   src_audio.duration)
                if e > s:
                    src_audio.subclip(s, e).write_audiofile(p, logger=None)
                    tts_paths.append(p)
                else:
                    tts_paths.append(None)

        elif use_tts:
            update("running", 30, f"กำลังสร้างเสียงพากย์ {len(scenes)} scene...")
            for sc in scenes:
                p = str(OUTPUT_DIR / f"{job_id}_tts_{sc['scene']}.mp3")
                if not tts_edge(sc["text"], tts_voice, p):
                    print(f"Scene {sc['scene']}: Edge TTS ไม่ได้ → ใช้ gTTS แทน")
                    tts_gtts(sc["text"], p)
                tts_paths.append(p)

        # ── ตัดต่อวิดีโอ ──────────────────────────────────────────────────────
        update("running", 55, "กำลังตัดต่อวิดีโอ...")
        clips = []
        for i, sc in enumerate(scenes):
            s = min(sc["start"], src.duration - 0.1)
            e = min(sc["end"],   src.duration)
            if e <= s:
                continue
            clip = src.subclip(s, e)

            if i < len(tts_paths) and tts_paths[i]:
                try:
                    aud = AudioFileClip(tts_paths[i])
                    open_clips.append(aud)
                    if aud.duration > clip.duration:
                        aud = aud.subclip(0, clip.duration)
                    clip = clip.set_audio(aud)
                except Exception as e:
                    print(f"Audio attach error scene {i+1}: {e}")

            if use_subs:
                sub = make_sub_clip(sc["text"], clip.w, clip.duration)
                if sub:
                    clip = CompositeVideoClip([clip, sub])

            clips.append(clip)

        if not clips:
            raise ValueError("ไม่มีคลิปที่ตัดได้ กรุณาตรวจสอบไฟล์วิดีโอ")

        update("running", 75, f"กำลัง render {len(clips)} scene...")
        final = concatenate_videoclips(clips, method="compose")
        out_path = str(OUTPUT_DIR / f"{job_id}.mp4")
        final.write_videofile(out_path, codec="libx264", audio_codec="aac",
                              temp_audiofile=str(OUTPUT_DIR / f"tmp_{job_id}.m4a"),
                              remove_temp=True, logger=None)

        update("done", 100, "เสร็จแล้ว! คลิกดาวน์โหลดได้เลย", output=f"{job_id}.mp4")

    except Exception as ex:
        update("error", 0, f"เกิดข้อผิดพลาด: {str(ex)}")

    finally:
        for c in open_clips:
            try: c.close()
            except: pass
        for p in tts_paths:
            if p:
                try: os.remove(p)
                except: pass


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/voices")
def voices():
    return jsonify([{"id": k, "label": v} for k, v in THAI_VOICES.items()])

@app.route("/render", methods=["POST"])
def render():
    video       = request.files.get("video")
    mode        = request.form.get("mode", "tts")
    script_text = request.form.get("script", "").strip()
    use_tts     = request.form.get("tts") == "true"
    use_subs    = request.form.get("subtitles") == "true"
    tts_voice   = request.form.get("voice", "th-TH-PremwadeeNeural")
    api_key     = request.form.get("api_key", "")
    audio_file  = request.files.get("audio")

    if not video:
        return jsonify({"error": "กรุณาอัปโหลดวิดีโอ"}), 400
    if mode == "tts" and not script_text:
        return jsonify({"error": "กรุณาใส่สคริป"}), 400
    if mode == "audio" and not audio_file:
        return jsonify({"error": "กรุณาอัปโหลดไฟล์เสียง"}), 400

    if tts_voice not in THAI_VOICES:
        tts_voice = "th-TH-PremwadeeNeural"

    job_id = str(uuid.uuid4())[:8]
    ext = Path(video.filename).suffix or ".mp4"
    vid_path = str(UPLOAD_DIR / f"{job_id}{ext}")
    video.save(vid_path)

    audio_path = None
    if mode == "audio" and audio_file:
        a_ext = Path(audio_file.filename).suffix or ".mp3"
        audio_path = str(UPLOAD_DIR / f"{job_id}_audio{a_ext}")
        audio_file.save(audio_path)

    jobs[job_id] = {"status": "queued", "progress": 0, "message": "รอดำเนินการ...", "output": None}
    threading.Thread(target=build_job, args=(
        job_id, vid_path, script_text, use_tts, use_subs, tts_voice, api_key, audio_path
    ), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/status/<job_id>")
def status(job_id):
    return jsonify(jobs.get(job_id, {"status": "not_found"}))

@app.route("/download/<filename>")
def download(filename):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True,
                               download_name="output.mp4")

if __name__ == "__main__":
    print("🎬  Video Editor running at http://localhost:5000")
    print(f"🗣️  Thai voices: {', '.join(THAI_VOICES.keys())}")
    app.run(debug=False, port=5000)
