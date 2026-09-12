"""
Upload server - accepts video uploads and creates Cloudflare download links.

Run:
    uvicorn upload_server:app --host 0.0.0.0 --port 8766

Env:
    UPLOAD_DIR - where to save uploads
    DISCORD_WEBHOOK_URL - optional webhook to send results back to Discord
"""

import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
import aiohttp

from public_download import create_public_download_link

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "D:/Auto Clips for Kick/uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

app = FastAPI()


UPLOAD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Upload Video</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .container {
            background: white;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            padding: 40px;
            max-width: 600px;
            width: 100%;
        }
        h1 { color: #333; text-align: center; margin-bottom: 10px; font-size: 28px; }
        .subtitle { text-align: center; color: #666; margin-bottom: 30px; font-size: 14px; }
        .upload-area {
            border: 3px dashed #667eea;
            border-radius: 15px;
            padding: 40px 20px;
            text-align: center;
            background: #f8f9ff;
            transition: all 0.3s ease;
            cursor: pointer;
        }
        .upload-area:hover { border-color: #764ba2; background: #f0f2ff; }
        .upload-area.dragover { border-color: #764ba2; background: #e8ebff; }
        .upload-icon { font-size: 48px; margin-bottom: 15px; }
        .upload-text { font-size: 16px; color: #333; margin-bottom: 10px; font-weight: 500; }
        .upload-hint { font-size: 12px; color: #999; margin-bottom: 20px; }
        input[type="file"] { display: none; }
        .upload-btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 12px 30px;
            border-radius: 25px;
            font-size: 16px;
            cursor: pointer;
            font-weight: 500;
        }
        .upload-btn:hover { opacity: 0.9; }
        .upload-btn:disabled { background: #ccc; cursor: not-allowed; }
        .file-info {
            margin-top: 20px;
            padding: 15px;
            background: #f0f2ff;
            border-radius: 10px;
            display: none;
        }
        .file-info.show { display: block; }
        .progress-bar {
            width: 100%;
            height: 8px;
            background: #e0e0e0;
            border-radius: 4px;
            overflow: hidden;
            margin-top: 10px;
            display: none;
        }
        .progress-bar.show { display: block; }
        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #667eea 0%, #764ba2 100%);
            border-radius: 4px;
            transition: width 0.3s ease;
            width: 0%;
        }
        .result {
            margin-top: 25px;
            padding: 20px;
            background: #f0f2ff;
            border-radius: 10px;
            border-left: 4px solid #667eea;
            display: none;
        }
        .result.show { display: block; }
        .result.success { border-left-color: #28a745; background: #f0fff4; }
        .result.error { border-left-color: #dc3545; background: #fff5f5; }
        .result a { color: #667eea; word-break: break-all; }
        .copy-btn {
            background: #667eea;
            color: white;
            border: none;
            padding: 5px 15px;
            border-radius: 5px;
            cursor: pointer;
            font-size: 12px;
            margin-left: 10px;
        }
        .back-btn { display: inline-block; margin-top: 15px; color: #667eea; text-decoration: none; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Upload Video</h1>
        <p class="subtitle">Upload a video file to get a download link</p>

        <div class="upload-area" id="uploadArea">
            <div class="upload-icon">Video</div>
            <div class="upload-text">Drag & drop your video here</div>
            <div class="upload-hint">or click to browse</div>
            <input type="file" id="fileInput" accept="video/*" required>
            <button class="upload-btn" id="uploadBtn" disabled>Select File</button>
        </div>

        <div class="file-info" id="fileInfo">
            <div class="upload-text" id="fileName"></div>
            <div class="upload-hint" id="fileSize"></div>
            <div class="progress-bar" id="progressBar">
                <div class="progress-fill" id="progressFill"></div>
            </div>
        </div>

        <div class="result" id="result">
            <h3 id="resultTitle"></h3>
            <p id="resultMessage"></p>
            <p id="resultLink"></p>
        </div>

        <a href="/" class="back-btn">Upload another file</a>
    </div>

    <script>
        const uploadArea = document.getElementById('uploadArea');
        const fileInput = document.getElementById('fileInput');
        const uploadBtn = document.getElementById('uploadBtn');
        const fileInfo = document.getElementById('fileInfo');
        const fileName = document.getElementById('fileName');
        const fileSize = document.getElementById('fileSize');
        const progressBar = document.getElementById('progressBar');
        const progressFill = document.getElementById('progressFill');
        const result = document.getElementById('result');
        const resultTitle = document.getElementById('resultTitle');
        const resultMessage = document.getElementById('resultMessage');
        const resultLink = document.getElementById('resultLink');

        let selectedFile = null;

        uploadArea.addEventListener('click', () => fileInput.click());
        uploadArea.addEventListener('dragover', (e) => { e.preventDefault(); uploadArea.classList.add('dragover'); });
        uploadArea.addEventListener('dragleave', () => uploadArea.classList.remove('dragover'));
        uploadArea.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadArea.classList.remove('dragover');
            if (e.dataTransfer.files.length > 0) handleFile(e.dataTransfer.files[0]);
        });
        fileInput.addEventListener('change', (e) => { if (e.target.files.length > 0) handleFile(e.target.files[0]); });
        uploadBtn.addEventListener('click', (e) => { e.stopPropagation(); fileInput.click(); });

        function handleFile(file) {
            selectedFile = file;
            fileName.textContent = file.name;
            fileSize.textContent = 'Size: ' + (file.size / (1024 * 1024)).toFixed(2) + ' MB';
            fileInfo.classList.add('show');
            uploadBtn.textContent = 'Upload Now';
            uploadBtn.disabled = false;
            result.classList.remove('show');
        }

        uploadBtn.addEventListener('click', async (e) => {
            if (!selectedFile) return;
            e.stopPropagation();
            uploadBtn.disabled = true;
            uploadBtn.textContent = 'Uploading...';
            progressBar.classList.add('show');
            result.classList.remove('show');

            const formData = new FormData();
            formData.append('file', selectedFile);

            try {
                const xhr = new XMLHttpRequest();
                xhr.open('POST', '/upload', true);
                xhr.upload.addEventListener('progress', (e) => {
                    if (e.lengthComputable) progressFill.style.width = ((e.loaded / e.total) * 100) + '%';
                });
                xhr.onload = () => {
                    if (xhr.status === 200) {
                        const data = JSON.parse(xhr.responseText);
                        resultTitle.textContent = 'Upload Complete!';
                        resultMessage.innerHTML = '<strong>File:</strong> ' + data.filename + '<br><strong>Size:</strong> ' + data.size_mb.toFixed(2) + ' MB';
                        resultLink.innerHTML = '<strong>Download link:</strong><br><a href="' + data.download_url + '" target="_blank">' + data.download_url + '</a><button class="copy-btn" onclick="copyToClipboard(\'' + data.download_url + '\')">Copy</button><br><small>This link expires in 60 minutes.</small>';
                        result.classList.add('show', 'success');
                    } else {
                        throw new Error('Upload failed');
                    }
                };
                xhr.onerror = () => { throw new Error('Upload failed'); };
                xhr.send(formData);
            } catch (error) {
                resultTitle.textContent = 'Upload Failed';
                resultMessage.textContent = error.message;
                result.classList.add('show', 'error');
            } finally {
                uploadBtn.disabled = false;
                uploadBtn.textContent = 'Upload Again';
                progressFill.style.width = '0%';
            }
        });

        function copyToClipboard(text) {
            navigator.clipboard.writeText(text).then(() => alert('Link copied!'));
        }
    </script>
</body>
</html>
"""

RESULT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Upload Result</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }
        .result { padding: 20px; background: #f0f0f0; border-radius: 5px; }
        .error { color: red; }
        .success { color: green; }
        a { color: #007bff; }
    </style>
</head>
<body>
    <h1>Upload Result</h1>
    <div class="result">
        <h2>Upload Complete!</h2>
        <p><strong>File:</strong> {{ filename }}</p>
        <p><strong>Size:</strong> {{ size_mb }} MB</p>
        {% if download_url %}
        <p class="success">Download link created!</p>
        <p><a href="{{ download_url }}" target="_blank">{{ download_url }}</a></p>
        <p><small>This link expires in 60 minutes.</small></p>
        {% else %}
        <p class="error">Could not create download link.</p>
        {% endif %}
    </div>
    <p><a href="/">Upload another file</a></p>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def upload_form(request: Request):
    return HTMLResponse(content=UPLOAD_HTML)


@app.post("/upload")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    user_id: Optional[str] = Form(None),
):
    file_id = str(uuid.uuid4())
    save_path = UPLOAD_DIR / f"{file_id}_{file.filename}"

    content = await file.read()
    with open(save_path, "wb") as f:
        f.write(content)

    download_url = await create_public_download_link(str(save_path))

    result = {
        "file_id": file_id,
        "filename": file.filename,
        "size_mb": round(len(content) / (1024 * 1024), 2),
        "download_url": download_url or "",
    }

    if download_url and DISCORD_WEBHOOK_URL:
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    DISCORD_WEBHOOK_URL,
                    json={
                        "content": (
                            f"Upload ready!\n"
                            f"File: {file.filename}\n"
                            f"Size: {result['size_mb']}MB\n\n"
                            f"Download link:\n{download_url}\n\n"
                            "Link expires in 60 minutes."
                        )
                    },
                    timeout=30,
                )
        except Exception:
            pass

    if "text/html" in request.headers.get("accept", ""):
        html = RESULT_HTML.replace("{{ filename }}", result["filename"]) \
            .replace("{{ size_mb }}", str(result["size_mb"])) \
            .replace("{% if download_url %}", "") \
            .replace("{% else %}", "") \
            .replace("{% endif %}", "")
        if result["download_url"]:
            html = html.replace(
                '<p class="success">Download link created!</p>',
                '<p class="success">Download link created!</p><p><a href="' + result["download_url"] + '" target="_blank">' + result["download_url"] + '</a></p>'
            )
        else:
            html = html.replace('<p class="success">Download link created!</p>', '<p class="error">Could not create download link.</p>')
        return HTMLResponse(content=html)

    return JSONResponse(content=result)


@app.get("/health")
async def health():
    return {"status": "ok"}
