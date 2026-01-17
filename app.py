import os
import base64
import time
import uuid
from datetime import datetime

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from supabase import create_client, Client
import google.generativeai as genai
from dotenv import load_dotenv

# =====================
# LOAD ENV
# =====================
load_dotenv()

SUPABASE_URL = os.getenv("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

app = Flask(__name__)
CORS(app)

# Initialize Supabase with the Service Role Key
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_API_KEY)

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/admin")
def admin_page():
    return render_template("admin.html")

# =====================
# PRE-CHECK WORKER ID
# =====================
@app.route("/precheck", methods=["POST"])
def precheck():
    data = request.get_json()
    worker_id = data.get("worker_id")
    if not worker_id:
        return jsonify({"status": "ERROR", "message": "Worker ID required"}), 400
    try:
        worker = supabase.table("workers").select("name").eq("worker_id", worker_id).execute()
        if not worker.data:
            return jsonify({"status": "NOT_FOUND", "message": "ID not registered"}), 404
        return jsonify({"status": "SUCCESS", "worker_name": worker.data[0].get("name", "Worker")})
    except Exception as e:
        return jsonify({"status": "ERROR", "message": str(e)}), 500

def verify_ppe_with_gemini(image_base64: str) -> str:
    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash-preview-09-2025",
        system_instruction="You are an expert Construction Safety Inspector. Check PPE: HELMET, VEST, GLOVES, BOOTS. Reply ONLY: PPE_OK, PPE_MISSING: [LIST], or NO_WORKER."
    )
    prompt = "Analyze frame for PPE compliance."
    try:
        # Note: In production, consider a local/managed PPE check to reduce API costs/latency
        response = model.generate_content([
            {"text": prompt},
            {"inline_data": {"mime_type": "image/jpeg", "data": image_base64}}
        ])
        return (response.text or "").strip().upper()
    except Exception:
        return "ERROR_GEMINI"

# =====================
# VERIFY ATTENDANCE
# =====================
@app.route("/verify", methods=["POST"])
def verify():
    data = request.get_json()
    worker_id = data.get("worker_id")
    image_data = data.get("image")

    try:
        raw_b64 = image_data.split(",")[1]
        image_bytes = base64.b64decode(raw_b64)
        file_name = f"auto_{worker_id}_{uuid.uuid4()}.jpg"

        supabase.storage.from_("ppe-images").upload(path=file_name, file=image_bytes, file_options={"content-type": "image/jpeg"})
        ai_result = verify_ppe_with_gemini(raw_b64)
        supabase.storage.from_("ppe-images").remove([file_name])

        if "NO_WORKER" in ai_result:
            return jsonify({"status": "RETRY", "message": "Worker not detected"})

        status = "PRESENT" if "PPE_OK" in ai_result else "ABSENT"
        missing = ai_result.replace("PPE_MISSING:", "").strip() if status == "ABSENT" else ""

        supabase.table("attendance").insert({
            "worker_id": worker_id,
            "attendance_status": status,
            "ppe_status": "PASSED" if status == "PRESENT" else "FAILED",
            "ppe_missing_items": missing,
            "ppe_image_url": None,
            "date": datetime.now().date().isoformat()
        }).execute()

        worker = supabase.table("workers").select("name").eq("worker_id", worker_id).execute()
        return jsonify({
            "status": status, 
            "worker_name": worker.data[0]['name'] if worker.data else "Worker", 
            "missing": missing
        })

    except Exception as e:
        return jsonify({"status": "ERROR", "message": str(e)}), 500

@app.route("/manual-upload", methods=["POST"])
def manual_upload():
    data = request.get_json()
    worker_id = data.get("worker_id")
    image_data = data.get("image")
    try:
        raw_b64 = image_data.split(",")[1]
        image_bytes = base64.b64decode(raw_b64)
        file_name = f"manual_{worker_id}_{uuid.uuid4()}.jpg"
        
        supabase.storage.from_("ppe-images").upload(path=file_name, file=image_bytes, file_options={"content-type": "image/jpeg"})
        
        supabase.table("attendance").insert({
            "worker_id": worker_id,
            "attendance_status": "PRESENT",
            "ppe_status": "MANUAL_VERIFIED",
            "ppe_missing_items": "Manual Override Confirmed",
            "ppe_image_url": None,
            "date": datetime.now().date().isoformat()
        }).execute()
        
        supabase.storage.from_("ppe-images").remove([file_name])
        
        return jsonify({"status": "SUCCESS"})
    except Exception as e:
        return jsonify({"status": "ERROR", "message": str(e)}), 500

# =====================
# ADMIN API
# =====================

# Manual mark status from Admin Panel
@app.route("/api/manual-mark", methods=["POST"])
def manual_mark():
    data = request.get_json()
    worker_id = data.get("worker_id")
    status = data.get("status", "PRESENT")
    date_str = data.get("date", datetime.now().date().isoformat())
    
    try:
        supabase.table("attendance").insert({
            "worker_id": worker_id,
            "attendance_status": status,
            "ppe_status": "ADMIN_OVERRIDE",
            "ppe_missing_items": f"Marked {status} by Admin",
            "ppe_image_url": None,
            "date": date_str
        }).execute()
        return jsonify({"status": "SUCCESS"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stats", methods=["GET"])
def get_stats():
    # Allow filtering stats by date
    target_date = request.args.get('date', datetime.now().date().isoformat())
    try:
        # Fetch all logs for specified date, ordered by creation time
        logs = supabase.table("attendance").select("worker_id, attendance_status, created_at").eq("date", target_date).order("created_at").execute()
        
        # Track the LATEST status for each worker who interacted with the system on that date
        latest_status_map = {}
        for l in logs.data:
            latest_status_map[l['worker_id']] = l['attendance_status']
        
        # Count based on the latest state
        present_count = sum(1 for status in latest_status_map.values() if status == 'PRESENT')
        absent_count = sum(1 for status in latest_status_map.values() if status == 'ABSENT')
        
        # Fetch total registered workers
        total_workers_in_db = supabase.table("workers").select("id", count="exact").execute().count
        
        return jsonify({
            "present": present_count, 
            "absent": absent_count, 
            "total_workers": total_workers_in_db,
            "date": target_date
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/logs", methods=["GET"])
def get_logs():
    target_date = request.args.get('date')
    try:
        query = supabase.table("attendance").select("worker_id, attendance_status, ppe_status, ppe_missing_items, created_at, date")
        if target_date:
            query = query.eq("date", target_date)
        
        logs = query.order("created_at", desc=True).execute()
        return jsonify(logs.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/workers", methods=["GET"])
def get_workers():
    try:
        workers = supabase.table("workers").select("*").execute()
        return jsonify(workers.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/workers", methods=["POST"])
def add_worker():
    data = request.get_json()
    name = data.get("name")
    if not name:
        return jsonify({"error": "Name is required"}), 400
        
    try:
        res = supabase.table("workers").select("worker_id").execute()
        next_id = 1001
        if res.data:
            ids = []
            for item in res.data:
                try:
                    ids.append(int(item['worker_id']))
                except:
                    continue
            if ids:
                next_id = max(ids) + 1
        
        res = supabase.table("workers").insert({
            "worker_id": str(next_id), 
            "name": name
        }).execute()
        
        return jsonify({"status": "ok", "worker_id": str(next_id)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/workers/<worker_id>", methods=["DELETE"])
def delete_worker(worker_id):
    try:
        # Cascade delete logs locally as well to be safe
        supabase.table("attendance").delete().eq("worker_id", worker_id).execute()
        supabase.table("workers").delete().eq("worker_id", worker_id).execute()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
