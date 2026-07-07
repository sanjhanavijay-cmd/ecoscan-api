"""
EcoScan Cloud API — deployed to Railway
Handles AI analysis and phone validation for EcoScan APK
"""

from flask import Flask, request, jsonify
import google.generativeai as genai
from PIL import Image
import io, re, base64, os, json, hashlib
from datetime import datetime
import firebase_admin
from firebase_admin import credentials as fb_creds, db as fb_db

# ── Config ──────────────────────────────────────────────────────────────────
GEMINI_API_KEY  = os.environ.get('GEMINI_API_KEY', 'AIzaSyByniWxqOLfX51z1yHSLH1-1vOWh8v9ojo')
FIREBASE_DB_URL = os.environ.get('FIREBASE_DB_URL', 'https://earnx-db-default-rtdb.firebaseio.com')

# Firebase init from env var (JSON string) or local file
_fb_ok = False
try:
    sa_json = os.environ.get('FIREBASE_SA_JSON')
    if sa_json:
        sa_dict = json.loads(sa_json)
        firebase_admin.initialize_app(fb_creds.Certificate(sa_dict), {'databaseURL': FIREBASE_DB_URL})
    else:
        _sa_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'serviceAccountKey.json')
        firebase_admin.initialize_app(fb_creds.Certificate(_sa_path), {'databaseURL': FIREBASE_DB_URL})
    _fb_ok = True
    print("Firebase connected!")
except Exception as e:
    print(f"Firebase init failed: {e}")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

app = Flask(__name__)

@app.after_request
def cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

@app.route('/api/<path:p>', methods=['OPTIONS'])
@app.route('/<path:p>', methods=['OPTIONS'])
def options(p): return '', 200

# ── Helpers ──────────────────────────────────────────────────────────────────

def extract_section(title, text):
    lines = text.splitlines(); collecting = False; content = []
    for line in lines:
        if title.lower() in line.lower():
            collecting = True
            val = line.split(":", 1)[-1].strip()
            if val: content.append(val)
            continue
        if collecting:
            if ":" in line: break
            clean = re.sub(r'^\d+\.\s*', '', line.replace("*","").replace("-","").strip())
            if clean: content.append(clean)
    return content

def simplify_materials(lst, n=5):
    out = []
    for item in lst:
        w = re.sub(r"\(.*?\)", "", item).split(",")[0].strip().title()
        if w and w not in out: out.append(w)
    while len(out) < n: out.append("—")
    return out[:n]

def extract_value(text):
    text = text.replace(",","").replace("₹","")
    r = re.findall(r'(\d+)\s*[-–]\s*(\d+)', text)
    if r: return max(int(h) for _, h in r)
    vals = [int(v) for v in re.findall(r'\b\d+\b', text) if int(v) < 100000]
    return max(vals) if vals else None

def estimate_weight(name, cat):
    n = (name or "").lower()
    if "laptop" in n or "notebook" in n: return 2.5
    if "desktop" in n or "cpu" in n: return 8.0
    if "monitor" in n or "screen" in n: return 5.0
    if "mobile" in n or "phone" in n or "smartphone" in n: return 0.2
    if "tablet" in n: return 0.5
    if "keyboard" in n: return 0.8
    if "mouse" in n: return 0.1
    if "printer" in n: return 6.0
    if "battery" in n: return 0.3
    if "cable" in n or "charger" in n: return 0.2
    if "hard drive" in n or "hdd" in n or "ssd" in n: return 0.5
    return 1.0

def normalize_phone(p):
    p = (p or "").strip().replace(" ","").replace("-","").replace("+91","").replace("+","")
    if p.startswith("91") and len(p) == 12: p = p[2:]
    if p.startswith("0") and len(p) == 11: p = p[1:]
    return p

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return jsonify({'status': 'EcoScan Cloud API running'})

@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.json or {}
    img_b64 = data.get('image', '')
    if not img_b64:
        return jsonify({'error': 'No image provided'}), 400
    try:
        img_bytes = base64.b64decode(img_b64)
        pil = Image.open(io.BytesIO(img_bytes)).convert('RGB')
        buf = io.BytesIO()
        pil.save(buf, format='JPEG', quality=85)
        buf.seek(0)

        prompt = """You are an e-waste recycling expert in India.
Respond using EXACTLY:

Object Name: ...
Category: ...
Recycling Value (in INR): ...
Recoverable Materials:
- ...
- ...
- ...
- ...
- ...
"""
        resp = model.generate_content([prompt, {"mime_type": "image/jpeg", "data": buf.getvalue()}])
        text = resp.text

        name      = (extract_section("Object Name", text) or ["Unknown E-Waste"])[0]
        category  = (extract_section("Category", text) or ["Unknown"])[0]
        materials = simplify_materials(extract_section("Recoverable Materials", text))
        value     = extract_value(text)
        weight    = estimate_weight(name, category)

        return jsonify({
            'success': True,
            'name': name, 'category': category,
            'materials': [m for m in materials if m != '—'],
            'value': value, 'weight_kg': weight
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/validate_phone', methods=['POST'])
def validate_phone():
    data  = request.json or {}
    phone = normalize_phone(data.get('phone', ''))
    item_type       = data.get('item_type', 'Unknown')
    weight_kg       = float(data.get('weight_kg', 1.0))
    estimated_value = int(data.get('estimated_value', 0))

    if not phone or len(phone) != 10 or not phone.isdigit():
        return jsonify({'success': False, 'message': 'Invalid phone number'})

    if not _fb_ok:
        return jsonify({'success': False, 'message': 'Firebase not connected'})

    try:
        user_ref  = fb_db.reference(f'users/{phone}')
        user_data = user_ref.get()
        if not user_data:
            return jsonify({'success': False, 'message': 'Phone not registered in EarnX'})

        new_pts = user_data.get('total_points', 0) + estimated_value
        user_ref.update({'total_points': new_pts})
        fb_db.reference(f'contributions/{phone}').push({
            'item_type':         item_type,
            'points_earned':     estimated_value,
            'contribution_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        print(f"[Sync] {phone} +{estimated_value} RP → {new_pts}")
        return jsonify({
            'success': True,
            'message': f'Success! {estimated_value} points added.',
            'user_data': {
                'username':      user_data.get('username', 'User'),
                'points_earned': estimated_value,
                'new_points':    new_pts,
                'old_points':    user_data.get('total_points', 0),
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
