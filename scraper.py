import os, time, uuid, requests, pandas as pd
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

# --- ANTI-THROTTLE API CALLER ---
def get_carrier_info(mc_number, token, retries=5):
    params = {"type": "mc", "value": str(mc_number).strip(), "token": token}
    
    for attempt in range(retries):
        try:
            res = http_session.get(CARRIER_API_URL, params=params, timeout=12.0)
            
            if res.status_code == 200:
                data = res.json()
                return data
            elif res.status_code in [404, 400]:
                return {"not_found": True}
            elif res.status_code == 429:
                retry_after = res.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(float(retry_after))
                else:
                    time.sleep(4.0 * (attempt + 1))
                continue
            elif res.status_code in [500, 502, 503, 504]:
                if attempt < retries - 1:
                    time.sleep(3.0 * (attempt + 1))
                    continue
                return {"inactive_timeout": True}
            else:
                if attempt < retries - 1:
                    time.sleep(2.0 * (attempt + 1))
        except (requests.exceptions.Timeout, requests.exceptions.RequestException):
            if attempt < retries - 1:
                time.sleep(2.5 * (attempt + 1))
                
    return "API_ERROR"

def parse_carrier_data(mc_number, raw_data):
    if raw_data == "API_ERROR":
        return {
            "MC Number": f"MC-{mc_number}",
            "Carrier Name": "⚠️ API THROTTLED",
            "Entity Type": "N/A",
            "Operating Status": "⚠️ UNKNOWN",
            "Phone Number": "N/A",
            "Email Address": "N/A",
            "Location": "N/A"
        }

    if isinstance(raw_data, dict) and raw_data.get("inactive_timeout") is True:
        return {
            "MC Number": f"MC-{mc_number}",
            "Carrier Name": "INACTIVE / REVOKED DOCKET",
            "Entity Type": "N/A",
            "Operating Status": "🔴 INACTIVE",
            "Phone Number": "N/A",
            "Email Address": "N/A",
            "Location": "N/A"
        }

    if not raw_data or not isinstance(raw_data, dict) or raw_data.get("not_found") is True:
        return {
            "MC Number": f"MC-{mc_number}",
            "Carrier Name": "DOCKET NOT FOUND",
            "Entity Type": "N/A",
            "Operating Status": "❌ NOT FOUND",
            "Phone Number": "N/A",
            "Email Address": "N/A",
            "Location": "N/A"
        }

    if raw_data.get("error") or raw_data.get("message") == "Not Found":
        return {
            "MC Number": f"MC-{mc_number}",
            "Carrier Name": "DOCKET NOT FOUND",
            "Entity Type": "N/A",
            "Operating Status": "❌ NOT FOUND",
            "Phone Number": "N/A",
            "Email Address": "N/A",
            "Location": "N/A"
        }

    c = raw_data.get("carrier") or raw_data.get("data") or raw_data
    if not isinstance(c, dict) or not c:
        return {
            "MC Number": f"MC-{mc_number}",
            "Carrier Name": "DOCKET NOT FOUND",
            "Entity Type": "N/A",
            "Operating Status": "❌ NOT FOUND",
            "Phone Number": "N/A",
            "Email Address": "N/A",
            "Location": "N/A"
        }

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
        }

    def extract_status(val):
        if isinstance(val, dict):
            return str(val.get("status") or val.get("desc") or val.get("value") or "").upper()
        return str(val or "").upper()

    def is_strictly_active(val):
        v = extract_status(val)
        if not v or v in ["NONE", "NULL", "N/A", "NO", "FALSE", "DENIED", "N", "REVOKED", "INACTIVE", "DISCONTINUED", "NONE ON FILE"]:
            return False
        if any(neg in v for neg in ["INACTIVE", "REVOKED", "DISCONTINUED", "NONE", "NO", "FALSE", "DENIED", "NOT AUTHORIZED"]):
            return False
        return v in ["A", "Y", "TRUE", "ACTIVE", "AUTHORIZED", "GRANTED"] or any(pos in v for pos in ["ACTIVE", "GRANTED", "AUTH"])

    common_auth = c.get("common_authority_status") or c.get("commonAuthStatus") or c.get("common_authority") or c.get("common_status")
    contract_auth = c.get("contract_authority_status") or c.get("contractAuthStatus") or c.get("contract_authority") or c.get("contract_status")
    broker_auth = c.get("broker_authority_status") or c.get("brokerAuthStatus") or c.get("broker_authority") or c.get("broker_status") or c.get("brokerAuth")

    has_common_active = is_strictly_active(common_auth)
    has_contract_active = is_strictly_active(contract_auth)
    has_broker_active = is_strictly_active(broker_auth)

    allowed_op = str(c.get("allowed_to_operate") or c.get("allowedToOperate") or "").strip().upper()
    status_field = str(c.get("status") or c.get("status_code") or c.get("statusCode") or c.get("operating_status") or "").strip().upper()

    # --- TOP-DOWN OPERATING STATUS CHECK ---
    if allowed_op in ["N", "NO", "FALSE"] or status_field in ["I", "INACTIVE", "REVOKED", "NOT ACTIVE", "SUSPENDED", "NONE"]:
        is_active = False
    elif allowed_op in ["Y", "YES", "TRUE"] or status_field in ["A", "ACTIVE", "AUTHORIZED"]:
        is_active = True
    else:
        # If no explicit active flag is set, require at least one active authority
        is_active = has_common_active or has_contract_active or has_broker_active

    status_str = "🟢 ACTIVE" if is_active else "🔴 INACTIVE"

    # --- BULLETPROOF CLASSIFICATION ENGINE ---
    def flatten_dict_values(d):
        vals = []
        for v in d.values():
            if isinstance(v, dict):
                vals.extend(flatten_dict_values(v))
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        vals.extend(flatten_dict_values(item))
                    else:
                        vals.append(str(item))
            else:
                vals.append(str(v))
        return vals

    all_payload_text = " ".join(flatten_dict_values(c)).upper()
    explicit_entity_type = str(c.get("entity_type") or c.get("entityType") or c.get("type") or "").strip().upper()

    major_brokers = [
        "CH ROBINSON", "C.H. ROBINSON", "TQL", "TOTAL QUALITY LOGISTICS", 
        "RXO", "COYOTE LOGISTICS", "JB HUNT", "ECHO GLOBAL LOGISTICS", "ECHO GLOBAL"
    ]
    
    if "BROKER" in explicit_entity_type or any(b in name for b in major_brokers) or "BROKER" in all_payload_text or has_broker_active:
        entity_label = "BROKER"
    else:
        entity_label = "CARRIER"

    phone = str(c.get("phone") or c.get("cell_phone") or "N/A").strip()
    if phone in ["None", "null", ""]: phone = "N/A"

    email = str(c.get("email_address") or c.get("email") or "").strip()
    email_val = email if email and email.lower() not in ["none", "null", "not listed", ""] else "Not Listed"

    city = str(c.get("phy_city") or c.get("city") or "").strip()
    state = str(c.get("phy_state") or c.get("state") or "").strip()
    location = f"{city}, {state}".strip(", ") if city or state else "N/A"

    return {
        "MC Number": f"MC-{mc_number}",
        "Carrier Name": name,
        "Entity Type": entity_label,
        "Operating Status": status_str,
        "Phone Number": phone,
        "Email Address": email_val,
        "Location": location
    }

# --- STATE INIT ---
for key, val in [("authenticated", False), ("current_user", None), ("session_token", None), 
                 ("is_admin", False), ("login_time", None), ("running", False), 
                 ("scraped_rows", []), ("current_mc", ""), ("last_db_check", 0.0), ("last_session_check", 0.0),
                 ("auto_retry_enabled", True), ("target_batch_size", 25), ("batch_progress", 0)]:
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
    st.session_state.batch_progress = 0

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
                                     "scraped_rows": [], "current_mc": "", "batch_progress": 0})
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
                "email": u_email, 
                "password": u_pass, 
                "is_admin": (u_role == "Super Admin"), 
                "delay_ms": u_delay, 
                "session_duration_hours": u_hrs
            }).execute()
            st.success(f"Registered new account for {u_email}!")
            st.rerun()

        st.markdown("---")
        st.subheader("✏️ Edit Account Settings & Passwords")
        user_list = supabase.table("users").select("*").execute().data
        
        if user_list:
            user_emails = [u["email"] for u in user_list]
            target_email = st.selectbox("Choose Account to Modify:", user_emails)
            target_user = next((u for u in user_list if u["email"] == target_email), None)
            
            if target_user:
                col_e1, col_e2, col_e3 = st.columns(3)
                e_pass = col_e1.text_input("Change Password:", value=str(target_user.get("password", "")))
                e_hrs = col_e2.number_input("Session Lockout Timeout (Hours):", min_value=0.1, max_value=24.0, value=float(target_user.get("session_duration_hours", 3.0)), step=0.5)
                e_delay = col_e3.number_input("Speed Limit (ms):", min_value=1.0, value=float(target_user.get("delay_ms", 500.0)), step=10.0)
                
                if st.button("💾 Apply Account Updates", use_container_width=True):
                    supabase.table("users").update({
                        "password": e_pass,
                        "session_duration_hours": float(e_hrs),
                        "delay_ms": float(e_delay)
                    }).eq("email", target_email).execute()
                    st.success(f"Successfully updated credentials and timeouts for {target_email}!")
                    st.session_state.last_db_check = 0.0
                    time.sleep(1)
                    st.rerun()

            st.markdown("---")
            st.subheader("📋 Registered Users Overview")
            st.dataframe(pd.DataFrame(user_list)[["email", "is_admin", "password", "delay_ms", "session_duration_hours"]], use_container_width=True)
            
            st.subheader("🗑️ Delete Account")
            del_email = st.selectbox("Select Account to Delete:", [u for u in user_emails if u != st.session_state.current_user])
            if st.button("Delete Account", type="primary"):
                supabase.table("users").delete().eq("email", del_email).execute()
                st.success(f"Deleted {del_email}.")
                st.rerun()

    with t2:
        st.subheader("📊 Target User Activity History")
        logs = supabase.table("activity_logs").select("*").order("created_at", desc=True).limit(500).execute().data
        
        if logs:
            logs_df = pd.DataFrame(logs)
            logs_df["Time & Date"] = pd.to_datetime(logs_df["created_at"]).dt.strftime('%Y-%m-%d %I:%M:%S %p')
            
            log_users = ["ALL Users"] + sorted([str(u) for u in logs_df["email"].unique() if u])
            selected_log_user = st.selectbox("🔍 Filter Activity History by User:", log_users)
            
            display_logs = logs_df[logs_df["email"] == selected_log_user] if selected_log_user != "ALL Users" else logs_df

            st.caption(f"Showing **{len(display_logs)}** log records.")
            st.dataframe(display_logs[["Time & Date", "email", "action", "detail"]].rename(columns={
                "email": "User Email",
                "action": "Action",
                "detail": "Details"
            }), use_container_width=True)
        else:
            st.info("No activity logs found.")

    with t3:
        st.subheader("⚙️ Global Speed Overrides")
        cfg = get_system_config()
        over = st.checkbox("Global Speed Override (Applies to all users)", value=cfg["override_global_speed"])
        g_speed = st.number_input("Global Delay (ms):", value=cfg["throttle_delay_ms"])
        if st.button("💾 Save Global Settings"):
            update_global_config(g_speed, over)
            st.success("Global configuration saved!")
            st.rerun()

# --- MAIN HARVESTER ENGINE ---
if not show_admin:
    st.title("🚚 Automated Carrier Harvester")
    st.sidebar.success("CarrierChk API Active" if CARRIER_TOKEN else "Missing API Token")

    c1, c2 = st.columns(2)
    with c1:
        if st.session_state.current_mc == "":
            raw_mc = st.text_input("Starting MC Number:", placeholder="e.g. 1066434")
            if raw_mc.isdigit(): st.session_state.current_mc = int(raw_mc)
        else:
            st.session_state.current_mc = st.number_input("Set MC Number:", min_value=1, value=int(st.session_state.current_mc), step=1)
    with c2:
        st.metric("Session Speed Enforced", speed_str)

    # --- ADVANCED BATCH & RETRY CONFIGURATION ---
    with st.expander("⚙️ Batch Limits & Auto-Retry Settings", expanded=True):
        col_ar1, col_ar2 = st.columns(2)
        
        st.session_state.target_batch_size = col_ar1.number_input(
            "Batch Size (MC Count Per Cycle):", 
            min_value=1, 
            max_value=500, 
            value=int(st.session_state.get("target_batch_size", 25)), 
            step=5
        )
        
        st.session_state.auto_retry_enabled = col_ar2.checkbox(
            "🔄 Auto-Retry Throttled MCs At End Of Each Batch", 
            value=st.session_state.get("auto_retry_enabled", True)
        )

    b1, b2, b3, b4 = st.columns(4)
    if b1.button("🚀 Start Sequence", use_container_width=True):
        if st.session_state.current_mc != "":
            st.session_state.batch_progress = 0
            st.session_state.running = True
            st.rerun()
        else: st.error("Enter MC Number first.")

    if b2.button("🛑 STOP Sequence", use_container_width=True):
        if st.session_state.running and st.session_state.batch_progress > 0:
            start_mc_batch = int(st.session_state.current_mc) - int(st.session_state.batch_progress)
            end_mc_batch = int(st.session_state.current_mc) - 1
            if end_mc_batch >= start_mc_batch:
                log_activity(
                    st.session_state.current_user,
                    "search_batch",
                    f"Searched MC-{start_mc_batch} to MC-{end_mc_batch}"
                )
        st.session_state.running = False
        st.session_state.batch_progress = 0
        st.success("Paused sequence.")

    if b3.button("♻️ Manual Retry Throttled", use_container_width=True):
        retried_count = 0
        for idx, row in enumerate(st.session_state.scraped_rows):
            c_name = str(row.get("Carrier Name", "")).upper()
            op_stat = str(row.get("Operating Status", "")).upper()
            
            if "THROTTLED" in c_name or "UNKNOWN" in op_stat or "API_ERROR" in c_name:
                mc_c = str(row["MC Number"]).replace("MC-", "").strip()
                st.session_state.scraped_rows[idx] = parse_carrier_data(mc_c, get_carrier_info(mc_c, CARRIER_TOKEN))
                retried_count += 1
                time.sleep(1.0)
                
        if retried_count > 0:
            st.success(f"Retried {retried_count} throttled record(s)!")
        else:
            st.info("No throttled records to retry.")
        st.rerun()

    if b4.button("🗑️ Clear Data", use_container_width=True):
        st.session_state.scraped_rows = []
        st.session_state.batch_progress = 0
        st.rerun()

    # --- SCRAPING LOOP ---
    if st.session_state.running and st.session_state.current_mc != "":
        st_box = st.empty()
        target_limit = int(st.session_state.get("target_batch_size", 25))

        for _ in range(5):
            if not st.session_state.running: break
            
            target = str(st.session_state.current_mc)
            st.session_state.batch_progress += 1
            current_batch_count = st.session_state.batch_progress

            st_box.info(f"⚡ Live Scraping | Target: **MC-{target}** (Item {current_batch_count} of {target_limit} in current batch)...")
            
            raw_info = get_carrier_info(target, CARRIER_TOKEN)
            parsed_record = parse_carrier_data(target, raw_info)
            st.session_state.scraped_rows.append(parsed_record)
            st.session_state.current_mc += 1

            # BATCH COMPLETE CHECKPOINT
            if current_batch_count >= target_limit:
                start_mc_batch = int(st.session_state.current_mc) - target_limit
                end_mc_batch = int(st.session_state.current_mc) - 1

                if st.session_state.get("auto_retry_enabled", True):
                    total_scraped = len(st.session_state.scraped_rows)
                    start_idx = max(0, total_scraped - target_limit)

                    max_retry_passes = 3
                    for pass_num in range(1, max_retry_passes + 1):
                        batch_slice = st.session_state.scraped_rows[start_idx:]
                        throttled_indices = [
                            start_idx + i for i, r in enumerate(batch_slice)
                            if "THROTTLED" in str(r.get("Carrier Name", "")).upper() 
                            or "UNKNOWN" in str(r.get("Operating Status", "")).upper()
                        ]

                        if not throttled_indices:
                            break

                        cool_down = 4.0 * pass_num
                        st_box.warning(
                            f"🛑 Batch complete. Found {len(throttled_indices)} throttled record(s). "
                            f"Retry Pass {pass_num}/{max_retry_passes}: Cooling down {cool_down:.1f}s..."
                        )
                        time.sleep(cool_down)

                        for count, idx in enumerate(throttled_indices, start=1):
                            retry_mc = str(st.session_state.scraped_rows[idx]["MC Number"]).replace("MC-", "").strip()
                            st_box.info(f"🔄 Retrying MC-{retry_mc} (Pass {pass_num} | {count}/{len(throttled_indices)})...")

                            new_raw = get_carrier_info(retry_mc, CARRIER_TOKEN)
                            new_parsed = parse_carrier_data(retry_mc, new_raw)
                            st.session_state.scraped_rows[idx] = new_parsed
                            time.sleep(2.0)

                    final_slice = st.session_state.scraped_rows[start_idx:]
                    remaining_throttled = sum(
                        1 for r in final_slice 
                        if "THROTTLED" in str(r.get("Carrier Name", "")).upper() or "UNKNOWN" in str(r.get("Operating Status", "")).upper()
                    )

                    if remaining_throttled == 0:
                        st_box.success(f"✅ Batch complete! All MCs verified with 0 throttled errors. Continuing next batch starting from MC-{st.session_state.current_mc}...")
                    else:
                        st_box.warning(f"⚠️ Batch finished with {remaining_throttled} persistent throttle error(s). Continuing next batch starting from MC-{st.session_state.current_mc}...")

                    time.sleep(1.5)
                else:
                    st_box.info(f"🛑 Batch complete! Continuing next batch starting from MC-{st.session_state.current_mc}...")
                    time.sleep(1.5)

                log_activity(
                    st.session_state.current_user,
                    "search_batch",
                    f"Searched MC-{start_mc_batch} to MC-{end_mc_batch}"
                )

                st.session_state.batch_progress = 0
                st.rerun()
                break

            if "THROTTLED" in str(parsed_record.get("Carrier Name")).upper() or raw_info == "API_ERROR":
                st_box.warning(f"⚠️ Throttling detected on MC-{target}. Auto cooling down 4.0s...")
                time.sleep(4.0)
            else:
                time.sleep(max(0.3, delay_ms / 1000.0))
        st.rerun()

    # --- FILTERING & DISPLAY ---
    st.markdown("---")
    if st.session_state.scraped_rows:
        # Instant Session Correction Pass for Brokers & Inactive Statuses
        major_brokers = [
            "CH ROBINSON", "C.H. ROBINSON", "TQL", "TOTAL QUALITY LOGISTICS", 
            "RXO", "COYOTE LOGISTICS", "JB HUNT", "ECHO GLOBAL LOGISTICS", "ECHO GLOBAL"
        ]
        for r in st.session_state.scraped_rows:
            c_name = str(r.get("Carrier Name", "")).upper()
            if any(b in c_name for b in major_brokers):
                r["Entity Type"] = "BROKER"

        base_df = pd.DataFrame(st.session_state.scraped_rows)

        for col in ["Entity Type", "Operating Status", "Carrier Name", "MC Number", "Location", "Email Address"]:
            if col not in base_df.columns: base_df[col] = "N/A"
            base_df[col] = base_df[col].fillna("N/A").astype(str)

        with st.expander("🔍 Filter Collected Records", expanded=True):
            f1, f2, f3, f4 = st.columns(4)
            sq = f1.text_input("🔎 Search Name / MC:", value="").strip().lower()
            
            raw_ent = [e for e in base_df["Entity Type"].unique() if e not in ["N/A", "nan", "None", ""]]
            sel_ent = f2.selectbox("🚛 Filter Entity Type:", ["ALL"] + sorted(list(set(raw_ent))))

            raw_stat = [s for s in base_df["Operating Status"].unique() if s not in ["N/A", "nan", "None", ""]]
            sel_stat = f3.selectbox("📌 Filter Status:", ["ALL"] + sorted(list(set(raw_stat))))

            states = set(ALL_US_STATES)
            for loc in base_df["Location"]:
                if "," in loc:
                    st_code = loc.split(",")[-1].strip().upper()
                    if len(st_code) == 2: states.add(st_code)
            sel_state = f4.selectbox("📍 Filter State:", ["ALL"] + sorted(list(states)))

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
            if not leads_df.empty:
                st.dataframe(leads_df, use_container_width=True)
                st.download_button("📥 Export Clean Active Leads to CSV", leads_df.to_csv(index=False).encode('utf-8'), "Active_Leads.csv", "text/csv", use_container_width=True)
            else:
                st.info("No active leads matching current filters.")

        with tab3:
            emails = filtered_df[
                (filtered_df["Operating Status"].str.startswith("🟢 ACTIVE")) & 
                (filtered_df["Email Address"].str.contains("@", na=False)) &
                (~filtered_df["Email Address"].isin(["N/A", "Not Listed"]))
            ]["Email Address"].drop_duplicates()
            if not emails.empty:
                edf = pd.DataFrame({"Email Address": emails})
                st.dataframe(edf, use_container_width=True)
                st.text_area("Copy Emails:", value="\n".join(emails.tolist()), height=140)
                st.download_button("📥 Export Emails CSV", edf.to_csv(index=False).encode('utf-8'), "Active_Emails.csv", "text/csv", use_container_width=True)
            else:
                st.info("No active emails matching current filters.")
    else:
        st.info("No records collected yet. Click 'Start Sequence' to begin harvesting.")
