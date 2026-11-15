"""FaceSwap Lab personal backend. The Magic Hour API key stays server-side."""
from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent
MAGIC_HOUR_API_KEY = os.getenv("MAGIC_HOUR_API_KEY", "").strip()
MAGIC_HOUR_BASE = "https://api.magichour.ai/v1"
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", str(500 * 1024 * 1024)))
MAX_DURATION_SECONDS = 300
TIMEOUT = (20, 1800)

app = FastAPI(title="FaceSwap Lab", docs_url=None, redoc_url=None)


def auth_headers():
    if not MAGIC_HOUR_API_KEY:
        raise HTTPException(503, "ยังไม่ได้ตั้งค่า Magic Hour API key ฝั่งเซิร์ฟเวอร์")
    return {"Authorization": f"Bearer {MAGIC_HOUR_API_KEY}", "Content-Type": "application/json"}


def api_error(resp: requests.Response):
    try:
        payload = resp.json()
        detail = payload.get("message") or payload.get("error") or payload.get("detail") or str(payload)
    except Exception:
        detail = resp.text[:500]
    # Do not return request headers or secrets.
    raise HTTPException(resp.status_code if 400 <= resp.status_code < 500 else 502,
                        f"Magic Hour ตอบกลับ {resp.status_code}: {detail}")


def safe_ext(filename: str, allowed: set[str]) -> str:
    ext = Path(filename or "").suffix.lower().lstrip(".")
    if ext not in allowed:
        raise HTTPException(415, "ชนิดไฟล์ไม่รองรับ กรุณาใช้ไฟล์รูป JPG/PNG/WEBP และวิดีโอ MP4/MOV/WEBM")
    return ext


def check_size(upload: UploadFile):
    upload.file.seek(0, os.SEEK_END)
    size = upload.file.tell()
    upload.file.seek(0)
    if size == 0:
        raise HTTPException(400, "ไฟล์ว่างเปล่า")
    if size > MAX_FILE_BYTES:
        raise HTTPException(413, f"ไฟล์ใหญ่เกินกำหนด ({MAX_FILE_BYTES // (1024*1024)} MB)")


def request_upload_urls(image_ext: str, video_ext: str):
    res = requests.post(
        f"{MAGIC_HOUR_BASE}/files/upload-urls",
        headers=auth_headers(),
        json={"items": [{"type": "image", "extension": image_ext}, {"type": "video", "extension": video_ext}]},
        timeout=TIMEOUT,
    )
    if not res.ok:
        api_error(res)
    items = res.json().get("items", [])
    if len(items) < 2 or not all(i.get("upload_url") and i.get("file_path") for i in items[:2]):
        raise HTTPException(502, "ได้รับที่อยู่สำหรับอัปโหลดจากผู้ให้บริการไม่ครบ")
    return items[:2]


def upload_to_signed_url(upload: UploadFile, signed_url: str, mime_type: Optional[str]):
    upload.file.seek(0)
    # Match Magic Hour's signed-URL example: PUT raw bytes without auth headers.
    res = requests.put(signed_url, data=upload.file, timeout=TIMEOUT)
    if not res.ok:
        raise HTTPException(502, f"อัปโหลดไฟล์ไปยัง Magic Hour ไม่สำเร็จ ({res.status_code})")


@app.get("/api/status")
def status():
    return {
        "ready": bool(MAGIC_HOUR_API_KEY),
        "provider": "Magic Hour",
        "max_duration_seconds": MAX_DURATION_SECONDS,
        "max_file_bytes": MAX_FILE_BYTES,
        "message": "พร้อมใช้งาน" if MAGIC_HOUR_API_KEY else "รอใส่ MAGIC_HOUR_API_KEY ในค่าลับของเซิร์ฟเวอร์",
    }


@app.post("/api/process")
def create_face_swap(
    face_image: UploadFile = File(...),
    target_video: UploadFile = File(...),
    clip_seconds: float = Form(...),
    consent: str = Form(...),
):
    if consent.lower() not in {"yes", "true", "1", "on"}:
        raise HTTPException(400, "กรุณายืนยันว่ามีสิทธิ์ใช้รูปและวิดีโอนี้")
    if not 0.1 <= clip_seconds <= MAX_DURATION_SECONDS:
        raise HTTPException(400, "ช่วงคลิปต้องอยู่ระหว่าง 0.1 ถึง 300 วินาที")
    headers = auth_headers()
    image_ext = safe_ext(face_image.filename, {"jpg", "jpeg", "png", "webp"})
    video_ext = safe_ext(target_video.filename, {"mp4", "mov", "webm"})
    check_size(face_image)
    check_size(target_video)

    # Ask the provider for temporary signed upload URLs, then stream bytes to it.
    upload_items = request_upload_urls(image_ext, video_ext)
    upload_to_signed_url(face_image, upload_items[0]["upload_url"], face_image.content_type)
    upload_to_signed_url(target_video, upload_items[1]["upload_url"], target_video.content_type)

    payload = {
        "name": "FaceSwap Lab mobile job",
        "start_seconds": 0,
        "end_seconds": round(float(clip_seconds), 2),
        "style": {"version": "default"},
        "assets": {
            "face_swap_mode": "all-faces",
            "image_file_path": upload_items[0]["file_path"],
            "video_source": "file",
            "video_file_path": upload_items[1]["file_path"],
        },
    }
    res = requests.post(f"{MAGIC_HOUR_BASE}/face-swap", headers=headers, json=payload, timeout=TIMEOUT)
    if not res.ok:
        api_error(res)
    job = res.json()
    job_id = job.get("id")
    if not job_id:
        raise HTTPException(502, "สร้างงานแล้วแต่ผู้ให้บริการไม่ได้ส่งรหัสงานกลับมา")
    return {"id": job_id, "status": job.get("status", "queued"), "credits_charged": job.get("credits_charged")}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,100}", job_id):
        raise HTTPException(400, "รหัสงานไม่ถูกต้อง")
    res = requests.get(f"{MAGIC_HOUR_BASE}/video-projects/{job_id}", headers=auth_headers(), timeout=TIMEOUT)
    if not res.ok:
        api_error(res)
    data = res.json()
    downloads = data.get("downloads") or []
    output_url = downloads[0].get("url") if downloads and isinstance(downloads[0], dict) else None
    return {
        "id": job_id,
        "status": data.get("status", "processing"),
        "credits_charged": data.get("credits_charged"),
        "error": data.get("error"),
        "output_url": output_url,
    }


@app.get("/")
def home():
    return FileResponse(BASE / "index.html")


app.mount("/", StaticFiles(directory=str(BASE), html=True), name="static")
