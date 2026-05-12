from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import subprocess, tempfile, shutil, os, re, json, urllib.request

app = Flask(__name__, static_folder='.')
CORS(app)

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/ping')
def ping():
    return jsonify({"status": "ok"})

@app.route('/videos')
def videos():
    channel_id = request.args.get('channelId', '')
    api_key = request.args.get('apiKey', '')
    if not channel_id or not api_key:
        return jsonify({"error": "channelId, apiKey 필요"}), 400

    url = (
        f"https://www.googleapis.com/youtube/v3/search"
        f"?key={api_key}&channelId={channel_id}"
        f"&part=snippet,id&order=date&maxResults=50&type=video"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as res:
            data = json.loads(res.read())
        if "error" in data:
            return jsonify({"error": data["error"]["message"]}), 400
        result = [
            {
                "videoId": item["id"]["videoId"],
                "title": item["snippet"]["title"],
                "publishedAt": item["snippet"]["publishedAt"],
            }
            for item in data.get("items", [])
        ]
        return jsonify({"videos": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/subtitle')
def subtitle():
    video_id = request.args.get('videoId', '')
    if not video_id:
        return jsonify({"error": "videoId 필요"}), 400

    tmpdir = tempfile.mkdtemp()
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-auto-sub",
            "--write-sub",
            "--sub-lang", "ko,en",
            "--sub-format", "vtt",
            "--output", os.path.join(tmpdir, "%(id)s"),
            url
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        for fname in os.listdir(tmpdir):
            if fname.endswith(".vtt"):
                with open(os.path.join(tmpdir, fname), encoding="utf-8") as f:
                    raw = f.read()
                lines = []
                for line in raw.splitlines():
                    line = line.strip()
                    if not line or "-->" in line:
                        continue
                    if line.startswith(("WEBVTT", "Kind:", "Language:")):
                        continue
                    line = re.sub(r"<[^>]+>", "", line)
                    if line and line not in lines[-1:]:
                        lines.append(line)
                return jsonify({"success": True, "text": "\n".join(lines), "videoId": video_id})

        return jsonify({"success": False, "error": "자막 없음", "videoId": video_id})

    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "시간 초과", "videoId": video_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "videoId": video_id})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    app.run(host='0.0.0.0', port=port)
