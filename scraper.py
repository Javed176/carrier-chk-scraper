import os, time, uuid, re, requests, threading, queue, pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from supabase import create_client, Client

st.set_page_config(page_title="Carrier Automation Portal", layout="wide")

# --- CONFIGURATION ---
SUPABASE_URL = st.secrets.get("SUPABASE_URL") or os.environ.get("SUPABASE_URL")
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY") or os.environ.get("SUPABASE_KEY")
CARRIER_TOKEN = st.secrets.get("CARRIER_TOKEN") or os.environ.get("CARRIER_TOKEN")
CARRIER_API_URL = st.secrets.get("CARRIER_API_URL") or os.environ.get("CARRIER_API_URL")

if not SUPABASE_URL or not SUPABASE_KEY:
    st.error("🔑 Missing SUPABASE_URL or SUPABASE_KEY in secrets.")
    st.stop()

ALL_US_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS",
    "KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY",
    "NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC"
]

@st.cache_resource
def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

@st.cache_resource
def get_http() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json"})
    return s

supabase = get_supabase()
http_session = get_http()

# --- BACKEND UTILITIES ---
def log_activity(email, action, detail=""):
    try:
        supabase.table("activity_logs").insert({"email": email, "action": action, "detail": detail}).execute()
    except Exception:
        pass

def get_system_config():
    config = {"throttle_delay_ms": 500.0, "override_global_speed": False}
    try:
        res = supabase.table("system_config").select("*").execute()
        for r in res.data:
            if r["key"] == "throttle_delay_ms": config["throttle_delay_ms"] = float(r["value"])
            elif r["key"] == "override_global_speed": config["override_global_speed"] = str(r["value"]).upper() == "TRUE"
    except Exception:
        pass
    return config

def update_global_config(delay_ms, override_bool):
    try:
        supabase.table("system_config").upsert({"key": "throttle_delay_ms", "value": f"{delay_ms:.4f}"}, on_conflict="key").execute()
        supabase.table("system_config").upsert({"key": "override_global_speed", "value": str(override_bool).upper()}, on_conflict="key").execute()
        return True
    except Exception as e:
        st.error(f"Config error: {e}")
        return False

def get_user_settings(email):
    try:
        res = supabase.table("users").select("delay_ms, session_duration_hours").eq("email", email).execute()
        if res.data:
            return float(res.data[0].get("delay_ms", 500.0)), float(res.data[0].get("session_duration_hours", 3.0))
    except Exception:
        pass
    return 500.0, 3.0

# --- API CALLER & PARSER ---
def call_api(mc_number, token):
    params = {"type": "mc", "value": str(mc_number).strip(), "token": token}
    try:
        res = http_session.get(CARRIER_API_URL, params=params, timeout=12.0)
        return res.status_code, res.headers, res.json() if res.status_code == 200 else res.text
    except Exception:
        return 500, {}, "ERROR"

def parse_carrier_data(mc_number, status_code, raw_data):
    if status_code == 429:
        return {
            "MC Number": f"MC-{mc_number}",
            "Carrier Name": "⚠️ API THROTTLED",
            "Entity Type": "N/A",
            "Operating Status": "⚠️ UNKNOWN",
            "Phone Number": "N/A",
            "Email Address": "N/A",
            "Location": "N/A"
        }, True # Flag as throttled

    if status_code in [404, 400] or raw_data == "ERROR":
        return {
            "MC Number": f"MC-{mc_number}",
            "Carrier Name": "DOCKET NOT FOUND",
            "Entity Type": "N/A",
            "Operating Status": "❌ NOT FOUND",
            "Phone Number": "N/A",
            "Email Address": "N/A",
            "Location": "N/A"
        }, False

    c = raw_data.get("carrier") or raw_data.get("data") or raw_data if isinstance(raw_data, dict) else {}
    name = str(c.get("dba_name") or c.get("legal_name") or c.get("name") or "N/A").strip().upper()
    if name in ["NONE", "NULL", "", "N/A", "NOT FOUND"]:
        return {
            "MC Number": f"MC-{mc_number}",
            "Carrier Name": "DOCKET NOT FOUND",
            "Entity Type": "N/A",
            "Operating Status": "❌ NOT FOUND",
            "Phone Number": "N/A",
            "Email Address": "N/A",
            "Location": "N/A"
        }, False

    def extract_status(val):
        if isinstance(val, dict):
            return str(val.get("status") or val.get("desc") or val.get("value") or "").upper()
        return str(val or "").upper()

    def is_strictly_active(val):
        v = extract_status(val)
        if not v or v in ["NONE", "NULL", "N/A", "NO", "FALSE", "DENIED", "N", "REVOKED", "INACTIVE", "DISCONTINUED"]:
            return False
        return v in ["A", "Y", "TRUE", "ACTIVE", "AUTHORIZED", "GRANTED"] or any(pos in v for pos in ["ACTIVE", "GRANTED", "AUTH"])

    common_auth = c.get("common_authority_status") or c.get("commonAuthStatus") or c.get("common_authority")
    contract_auth = c.get("contract_authority_status") or c.get("contractAuthStatus") or c.get("contract_authority")
    broker_auth = c.get("broker_authority_status") or c.get("brokerAuthStatus") or c.get("broker_authority")

    is_active = is_strictly_active(common_auth) or is_strictly_active(contract_auth) or is_strictly_active(broker_auth)
    status_str = "🟢 ACTIVE" if is_active else "🔴 INACTIVE"

    def flatten_dict_values(d):
        vals = []
        for v in d.values():
            if isinstance(v, dict): vals.extend(flatten_dict_values(v))
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict): vals.extend(flatten_dict_values(item))
                    else: vals.append(str(item))
            else: vals.append(str(v))
        return vals

    all_payload_text = " ".join(flatten_dict_values(c)).upper()
    explicit_entity_type = str(c.get("entity_type") or c.get("entityType") or "").strip().upper()
    
    major_brokers = {"CH ROBINSON": "chrobinson.com", "TQL": "tql.com", "RXO": "rxo.com", "COYOTE LOGISTICS": "coyote.com", "JB HUNT": "jbhunt.com"}
    is_broker = "BROKER" in explicit_entity_type or any(b in name for b in major_brokers) or is_strictly_active(broker_auth)
    entity_label = "BROKER" if is_broker else "CARRIER"

    phone = str(c.get("phone") or c.get("cell_phone") or "N/A").strip()
    if phone in ["None", "null", ""]: phone = "N/A"

    email = str(c.get("email_address") or c.get("email") or "").strip()
    if not email or email.lower() in ["none", "null", "not listed", ""]:
        emails_found = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', all_payload_text)
        valid_emails = [e for e in emails_found if not any(x in e.lower() for x in ["carrierchk", "example.com"])]
        email = valid_emails[0] if valid_emails else "Not Listed"

    city = str(c.get("phy_city") or c.get("city") or "").strip()
    state = str(c.get("phy_state") or c.get("state") or "").strip()
    location = f"{city}, {state}".strip(", ") if city or state else "N/A"

    return {
        "MC Number": f"MC-{mc_number}",
        "Carrier Name": name,
        "Entity Type": entity_label,
        "Operating Status": status_str,
        "Phone Number": phone,
        "Email Address": email,
        "Location": location
    }, False

# --- STATE INIT ---
for key, val in [("authenticated", False), ("current_user", None), ("session_token", None), 
                 ("is_admin", False), ("login_time", None), ("running", False), 
                 ("scraped_rows", []), ("current_mc", ""), ("last_db_check", 0.0), ("last_session_check", 0.0),
                 ("thread_status", "Idle"), ("active_mc_normal", "None"), ("active_mc_throttle", "None")]:
    if key not in st.session_state: st.session_state[key] = val

def force_logout(reason="Session Expired"):
    if st.session_state.authenticated and st.session_state.current_user:
        log_activity(st.session_state.current_user, "logout", reason)
        try: supabase.table("users").update({"active_session_id": None}).eq("email", st.session_state.current_user).execute()
        except Exception: pass
    for k in ["authenticated", "current_user", "session_token", "is_admin", "running"]:
        st.session_state[k] = False if isinstance(st.session_state[k], bool) else None
    st.session_state.scraped_rows = []
    st.session_state.current_mc = ""

def verify_active_session():
    if st.session_state.authenticated and st.session_state.current_user:
        now = time.time()
        if now - st.session_state.last_session_check < 30.0: return True
        st.session_state.last_session_check = now
        try:
            res = supabase.table("users").select("active_session_id").eq("email", st.session_state.current_user).execute()
            if res.data and res.data[0].get("active_session_id") != st.session_state.session_token:
                return False
        except Exception: pass
    return True

# --- LOGIN GATE ---
if not st.session_state.authenticated:
    st.title("🔒 Security Access Required")
    st.write("Enter credentials. Contact **my176business@gmail.com** or WhatsApp **+923097503520**")
    c1, c2 = st.columns(2)
    email_in = c1.text_input("Email:").strip().lower()
    pass_in = c2.text_input("Password:", type="password")
    if st.button("Verify & Unlock Engine", use_container_width=True):
        res = supabase.table("users").select("*").eq("email", email_in).execute()
        if res.data and res.data[0]["password"] == pass_in:
            token = str(uuid.uuid4())
            supabase.table("users").update({"active_session_id": token}).eq("email", email_in).execute()
            st.session_state.update({"authenticated": True, "current_user": email_in, "session_token": token,
                                     "is_admin": res.data[0].get("is_admin", False), "login_time": time.time(),
                                     "scraped_rows": [], "current_mc": ""})
            log_activity(email_in, "login", "Success")
            st.rerun()
        else: st.error("Access denied.")
    st.stop()

if not verify_active_session():
    st.error("⚠️ Logged in from another tab or device.")
    st.session_state.authenticated = False
    time.sleep(1.5)
    st.rerun()

# --- SPEED CONFIG & AUTO-LOCK ---
now = time.time()
if now - st.session_state.last_db_check > 30.0:
    cfg = get_system_config()
    if cfg["override_global_speed"]:
        st.session_state.cached_delay_ms = cfg["throttle_delay_ms"]
        st.session_state.cached_speed_str = f"🚨 Forced Override ({cfg['throttle_delay_ms']:.2f} ms)"
        _, st.session_state.cached_dur = get_user_settings(st.session_state.current_user)
    else:
        st.session_state.cached_delay_ms, st.session_state.cached_dur = get_user_settings(st.session_state.current_user)
        st.session_state.cached_speed_str = f"👤 {st.session_state.cached_delay_ms:.2f} ms"
    st.session_state.last_db_check = now

delay_ms = st.session_state.get("cached_delay_ms", 500.0)
session_dur = st.session_state.get("cached_dur", 3.0)
speed_str = st.session_state.get("cached_speed_str", "500 ms")

if st.session_state.login_time and (time.time() - st.session_state.login_time >= session_dur * 3600):
    force_logout("Auto-Expired")
    st.warning("⏱️ Session Expired.")
    st.rerun()

# --- SIDEBAR ---
st.sidebar.markdown(f"### 👤 Logged In As:\n`{st.session_state.current_user}`")
rem_sec = max(0, int((session_dur * 3600) - (time.time() - st.session_state.login_time)))
components.html(f"""
<div style="font-family:monospace;font-size:15px;font-weight:bold;color:#ff4b4b;background:#0e1117;padding:8px;border-radius:5px;text-align:center;border:1px solid #30363d;">
Auto-Locks In: <span id="clock">--</span>
</div>
<script>
    let rem = {rem_sec};
    function u(){{
        if(rem<=0){{ location.reload(); return; }}
        let h=Math.floor(rem/3600), m=Math.floor((rem%3600)/60), s=rem%60;
        document.getElementById('clock').textContent = (h<10?'0'+h:h)+'h '+(m<10?'0'+m:m)+'m '+(s<10?'0'+s:s)+'s';
        rem--;
    }}
    u(); setInterval(u, 1000);
</script>""", height=55)

if st.sidebar.button("🔓 Log Out", use_container_width=True):
    force_logout("Manual Logout")
    st.rerun()

show_admin = st.sidebar.checkbox("🛡️ Admin Dashboard", value=False) if st.session_state.is_admin else False

# --- ADMIN PANEL ---
if show_admin and st.session_state.is_admin:
    st.title("🛡️ Super Admin Control Dashboard")
    t1, t2, t3 = st.tabs(["👥 User Management", "📊 Activity History Logs", "⚙️ System Configuration"])
    
    with t1:
        st.subheader("➕ Register New User")
        col_a1, col_a2, col_a3 = st.columns(3)
        u_email = col_a1.text_input("New Email:").strip().lower()
        u_pass = col_a2.text_input("Set Password:")
        u_role = col_a3.selectbox("Role:", ["Standard User", "Super Admin"])
        
        col_a4, col_a5 = st.columns(2)
        u_delay = col_a4.number_input("Speed Limit (ms):", value=500.0, step=10.0)
        u_hrs = col_a5.number_input("Session Timeout (Hours):", value=3.0, step=0.5)
        
        if st.button("➕ Add User Account", use_container_width=True) and u_email and u_pass:
            supabase.table("users").insert({
                "email": u_email, "password": u_pass, "is_admin": (u_role == "Super Admin"), 
                "delay_ms": u_delay, "session_duration_hours": u_hrs
            }).execute()
            st.success(f"Registered new account for {u_email}!")
            st.rerun()

        st.markdown("---")
        st.subheader("📋 Registered Users Overview")
        user_list = supabase.table("users").select("*").execute().data
        if user_list:
            st.dataframe(pd.DataFrame(user_list)[["email", "is_admin", "delay_ms", "session_duration_hours"]], use_container_width=True)

    with t2:
        st.subheader("📊 Target User Activity History")
        logs = supabase.table("activity_logs").select("*").order("created_at", desc=True).limit(200).execute().data
        if logs:
            logs_df = pd.DataFrame(logs)
            st.dataframe(logs_df[["created_at", "email", "action", "detail"]], use_container_width=True)

    with t3:
        st.subheader("⚙️ Global Speed Overrides")
        cfg = get_system_config()
        over = st.checkbox("Global Speed Override", value=cfg["override_global_speed"])
        g_speed = st.number_input("Global Delay (ms):", value=cfg["throttle_delay_ms"])
        if st.button("💾 Save Global Settings"):
            update_global_config(g_speed, over)
            st.success("Saved!")
            st.rerun()

# --- MAIN TWO-THREAD HARVESTER ENGINE ---
if not show_admin:
    st.title("🚚 Automated Carrier Harvester (Dual-Thread Engine)")
    st.sidebar.success("CarrierChk API Active" if CARRIER_TOKEN else "Missing API Token")

    c1, c2 = st.columns(2)
    with c1:
        if st.session_state.current_mc == "":
            raw_mc = st.text_input("Starting MC Number:", placeholder="e.g. 1800000")
            if raw_mc.isdigit(): st.session_state.current_mc = int(raw_mc)
        else:
            st.session_state.current_mc = st.number_input("Set MC Number:", min_value=1, value=int(st.session_state.current_mc), step=1)
    with c2:
        st.metric("Session Speed Enforced", speed_str)

    b1, b2, b3 = st.columns(3)
    if b1.button("🚀 Start Live Engine", use_container_width=True):
        if st.session_state.current_mc != "":
            st.session_state.running = True
            st.rerun()
        else: st.error("Enter MC Number first.")

    if b2.button("🛑 STOP Engine", use_container_width=True):
        st.session_state.running = False
        st.success("Stopped harvesting engine.")

    if b3.button("🗑️ Clear Data", use_container_width=True):
        st.session_state.scraped_rows = []
        st.rerun()

    # --- DUAL-THREAD QUEUE SETUP ---
    if "q_normal" not in st.session_state: st.session_state.q_normal = queue.Queue()
    if "q_throttle" not in st.session_state: st.session_state.q_throttle = queue.Queue()
    if "data_lock" not in st.session_state: st.session_state.data_lock = threading.Lock()

    status_box = st.empty()

    # Background Workers
    def normal_worker():
        while st.session_state.running:
            try:
                mc = st.session_state.q_normal.get(timeout=0.5)
            except queue.Empty:
                continue
            
            st.session_state.active_mc_normal = f"MC-{mc}"
            code, headers, raw = call_api(mc, CARRIER_TOKEN)
            parsed, is_throttled = parse_carrier_data(mc, code, raw)

            if is_throttled:
                # Hand off immediately to Thread 2 (Throttle/Sleep Worker)
                st.session_state.q_throttle.put(mc)
            else:
                with st.session_state.data_lock:
                    st.session_state.scraped_rows.append(parsed)
                time.sleep(delay_ms / 1000.0)
            
            # Queue next sequential MC
            if st.session_state.running:
                st.session_state.current_mc = mc + 1
                st.session_state.q_normal.put(mc + 1)
            st.session_state.q_normal.task_done()

    def throttle_worker():
        while st.session_state.running:
            try:
                mc = st.session_state.q_throttle.get(timeout=0.5)
            except queue.Empty:
                continue
            
            st.session_state.active_mc_throttle = f"MC-{mc} (Throttled - Sleeping)"
            
            # Sleep & Backoff specifically for throttled requests
            backoff_sleep = 4.0
            time.sleep(backoff_sleep)
            
            code, headers, raw = call_api(mc, CARRIER_TOKEN)
            parsed, is_throttled = parse_carrier_data(mc, code, raw)

            if is_throttled:
                # Put back in throttle queue if still rate limited
                st.session_state.q_throttle.put(mc)
            else:
                with st.session_state.data_lock:
                    st.session_state.scraped_rows.append(parsed)
                st.session_state.active_mc_throttle = "Idle / Cleared"
            
            st.session_state.q_throttle.task_done()

    # Start threads if running
    if st.session_state.running:
        if st.session_state.q_normal.empty() and st.session_state.q_throttle.empty():
            st.session_state.q_normal.put(int(st.session_state.current_mc))
            
            t_normal = threading.Thread(target=normal_worker, daemon=True)
            t_throttle = threading.Thread(target=throttle_worker, daemon=True)
            t_normal.start()
            t_throttle.start()

        status_box.info(f"⚡ **Thread 1 (Normal):** Processing `{st.session_state.active_mc_normal}` | 🛑 **Thread 2 (Throttle/Sleep):** `{st.session_state.active_mc_throttle}`")
        time.sleep(1.0)
        st.rerun()

    # --- FILTERING & DISPLAY ---
    st.markdown("---")
    if st.session_state.scraped_rows:
        base_df = pd.DataFrame(st.session_state.scraped_rows)

        for col in ["Entity Type", "Operating Status", "Carrier Name", "MC Number", "Location", "Email Address"]:
            if col not in base_df.columns: base_df[col] = "N/A"
            base_df[col] = base_df[col].fillna("N/A").astype(str)

        with st.expander("🔍 Filter Collected Records", expanded=True):
            f1, f2, f3, f4 = st.columns(4)
            sq = f1.text_input("🔎 Search Name / MC:", value="").strip().lower()
            sel_ent = f2.selectbox("🚛 Filter Entity Type:", ["ALL"] + sorted(list(base_df["Entity Type"].unique())))
            sel_stat = f3.selectbox("📌 Filter Status:", ["ALL"] + sorted(list(base_df["Operating Status"].unique())))
            sel_state = f4.selectbox("📍 Filter State:", ["ALL"] + sorted(list(ALL_US_STATES)))

        filtered_df = base_df.copy()
        if sq:
            filtered_df = filtered_df[filtered_df["Carrier Name"].str.lower().str.contains(sq) | filtered_df["MC Number"].str.lower().str.contains(sq)]
        if sel_ent != "ALL":
            filtered_df = filtered_df[filtered_df["Entity Type"] == sel_ent]
        if sel_stat != "ALL":
            filtered_df = filtered_df[filtered_df["Operating Status"] == sel_stat]
        if sel_state != "ALL":
            filtered_df = filtered_df[filtered_df["Location"].str.endswith(sel_state)]

        st.caption(f"Showing **{len(filtered_df)}** of **{len(base_df)}** total harvested records.")

        tab1, tab2, tab3 = st.tabs(["📋 Complete Master Log", "🎯 Verified Leads (Active Only)", "📧 Raw Active Email List"])
        
        with tab1:
            st.dataframe(filtered_df, use_container_width=True)
            st.download_button("📥 Export Master Sheet to CSV", filtered_df.to_csv(index=False).encode('utf-8'), "Master_MC_Log.csv", "text/csv", use_container_width=True)

        with tab2:
            leads_df = filtered_df[
                (filtered_df["Operating Status"].str.startswith("🟢 ACTIVE")) & 
                (filtered_df["Email Address"].str.contains("@", na=False)) &
                (~filtered_df["Email Address"].isin(["N/A", "Not Listed"]))
            ]
            st.dataframe(leads_df, use_container_width=True)
            st.download_button("📥 Export Clean Active Leads to CSV", leads_df.to_csv(index=False).encode('utf-8'), "Active_Leads.csv", "text/csv", use_container_width=True)

        with tab3:
            emails = filtered_df[
                (filtered_df["Operating Status"].str.startswith("🟢 ACTIVE")) & 
                (filtered_df["Email Address"].str.contains("@", na=False)) &
                (~filtered_df["Email Address"].isin(["N/A", "Not Listed"]))
            ]["Email Address"].drop_duplicates()
            st.text_area("Copy Emails:", value="\n".join(emails.tolist()), height=140)
            st.download_button("📥 Export Emails CSV", pd.DataFrame({"Email Address": emails}).to_csv(index=False).encode('utf-8'), "Active_Emails.csv", "text/csv", use_container_width=True)
    else:
        st.info("No records collected yet. Click 'Start Live Engine' to begin harvesting.")
