import os, time, uuid, requests, pandas as pd
from datetime import datetime, timedelta
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
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
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
    config = {"throttle_delay_ms": 300.0, "override_global_speed": False}
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
            return float(res.data[0].get("delay_ms", 300.0)), float(res.data[0].get("session_duration_hours", 3.0))
    except Exception:
        pass
    return 300.0, 3.0

# --- API & DYNAMIC WAIT / 10-SEC TIMEOUT LOGIC ---
def get_carrier_info(mc_number, token, retries=1):
    params = {"type": "mc", "value": str(mc_number).strip(), "token": token}
    for attempt in range(retries + 1):
        try:
            # Dynamic response wait: moves on instantly as soon as data arrives, max 10s ceiling
            res = http_session.get(CARRIER_API_URL, params=params, timeout=10.0)
            
            if res.status_code == 200:
                data = res.json()
                return data
            elif res.status_code in [404, 400]:
                # Non-existent carrier dockets
                return {"not_found": True}
            elif res.status_code in [500, 502, 503, 504]:
                # FMCSA upstream timeout / gateway error on old inactive dockets
                return {"inactive_timeout": True}
            elif res.status_code == 429:
                # Rate limited
                time.sleep(1.2 * (attempt + 1))
                continue
            else:
                if attempt < retries:
                    time.sleep(0.5)
        except requests.exceptions.Timeout:
            # Reached full 10-second wait limit
            if attempt < retries:
                time.sleep(0.8)
        except requests.exceptions.RequestException:
            if attempt < retries:
                time.sleep(0.5)
                
    return "API_ERROR"

def parse_carrier_data(mc_number, raw_data):
    # 1. Genuine API Failure or Rate Limit after 10-sec timeout
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

    # 2. Inactive docket that timed out on FMCSA server side
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

    # 3. Non-existent MC / Docket Not Found (HTTP 404 or empty object)
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

    # Payload level error check
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

    # Extract Carrier / Business Name
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

    # Extract Raw Entity Type
    raw_entity = str(
        c.get("entity_type") or c.get("entity_type_desc") or c.get("entityType") or c.get("type") or c.get("operation_classification") or ""
    ).strip().upper()

    # Helper function to check explicit active authority status values
    def is_strictly_active(val):
        if not val: return False
        v = str(val).strip().upper()
        if any(neg in v for neg in ["INACTIVE", "REVOKED", "DISCONTINUED", "NONE", "NO", "FALSE", "DISMISS", "DENIED", "N"]):
            return False
        return v in ["A", "Y", "TRUE", "ACTIVE", "AUTHORIZED", "GRANTED"] or any(pos in v for pos in ["ACTIVE", "GRANTED", "AUTH"])

    # Authority Field Checks
    common_auth = c.get("common_authority_status") or c.get("commonAuthStatus") or c.get("common_authority") or c.get("common_status")
    contract_auth = c.get("contract_authority_status") or c.get("contractAuthStatus") or c.get("contract_authority") or c.get("contract_status")
    broker_auth = c.get("broker_authority_status") or c.get("brokerAuthStatus") or c.get("broker_authority") or c.get("broker_status")

    has_common_active = is_strictly_active(common_auth)
    has_contract_active = is_strictly_active(contract_auth)
    has_broker_active = is_strictly_active(broker_auth)

    # Power Units Check
    power_units_raw = c.get("power_units") if c.get("power_units") is not None else c.get("powerUnits")
    try:
        pu_count = int(power_units_raw)
    except (ValueError, TypeError):
        pu_count = None

    # Status Determination
    allowed_op = str(c.get("allowed_to_operate") or c.get("allowedToOperate") or "").strip().upper()
    status_field = str(c.get("status") or c.get("status_code") or c.get("statusCode") or c.get("operating_status") or "").strip().upper()

    is_active = (
        allowed_op in ["Y", "YES", "TRUE"] or
        status_field in ["A", "ACTIVE", "AUTHORIZED"] or
        has_common_active or
        has_contract_active or
        has_broker_active
    )

    # Override: Inactive status without active authority values
    if (status_field in ["I", "INACTIVE", "REVOKED", "NOT ACTIVE"] or allowed_op in ["N", "NO"]) and not (has_common_active or has_contract_active or has_broker_active):
        is_active = False

    status_str = "🟢 ACTIVE" if is_active else "🔴 INACTIVE"

    # Entity Type Determination
    is_broker = (has_broker_active or "BROKER" in raw_entity or "FORWARDER" in raw_entity)
    is_carrier = (has_common_active or has_contract_active or ("CARRIER" in raw_entity and "BROKER" not in raw_entity))

    # Zero fleet size rule: If fleet is 0 and no motor carrier authority, enforce BROKER
    if pu_count == 0 and not (has_common_active or has_contract_active):
        is_carrier = False
        if is_broker or "LOGISTICS" in name or "BROKER" in name or "FREIGHT" in name:
            is_broker = True

    if is_broker and is_carrier:
        entity_label = "CARRIER / BROKER"
    elif is_broker:
        entity_label = "BROKER"
    elif is_carrier:
        entity_label = "CARRIER"
    else:
        if "BROKER" in raw_entity or "LOGISTICS" in name or "BROKER" in name:
            entity_label = "BROKER"
        else:
            entity_label = "CARRIER"

    # Contact & Location
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
                 ("scraped_rows", []), ("current_mc", ""), ("last_db_check", 0.0), ("last_session_check", 0.0)]:
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

delay_ms = st.session_state.get("cached_delay_ms", 300.0)
session_dur = st.session_state.get("cached_dur", 3.0)
speed_str = st.session_state.get("cached_speed_str", "300 ms")

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
        u_delay = col_a4.number_input("Speed Limit (ms):", value=300.0, step=10.0)
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
                e_delay = col_e3.number_input("Speed Limit (ms):", min_value=1.0, value=float(target_user.get("delay_ms", 300.0)), step=10.0)
                
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

    b1, b2, b3, b4 = st.columns(4)
    if b1.button("🚀 Start Sequence", use_container_width=True):
        if st.session_state.current_mc != "":
            st.session_state.running = True
            st.rerun()
        else: st.error("Enter MC Number first.")

    if b2.button("🛑 STOP Sequence", use_container_width=True):
        st.session_state.running = False
        st.success("Paused sequence.")

    if b3.button("♻️ Retry Throttled", use_container_width=True):
        retried_count = 0
        for idx, row in enumerate(st.session_state.scraped_rows):
            c_name = str(row.get("Carrier Name", "")).upper()
            op_stat = str(row.get("Operating Status", "")).upper()
            
            if "THROTTLED" in c_name or "UNKNOWN" in op_stat or "API_ERROR" in c_name:
                mc_c = str(row["MC Number"]).replace("MC-", "").strip()
                st.session_state.scraped_rows[idx] = parse_carrier_data(mc_c, get_carrier_info(mc_c, CARRIER_TOKEN))
                retried_count += 1
                time.sleep(0.3)
                
        if retried_count > 0:
            st.success(f"Retried {retried_count} throttled record(s)!")
        else:
            st.info("No throttled records to retry.")
        st.rerun()

    if b4.button("🗑️ Clear Data", use_container_width=True):
        st.session_state.scraped_rows = []
        st.rerun()

    # --- SCRAPING LOOP ---
    if st.session_state.running and st.session_state.current_mc != "":
        st_box = st.empty()
        for _ in range(5):
            if not st.session_state.running: break
            target = str(st.session_state.current_mc)
            st_box.info(f"⚡ Live Scraping | Target: **MC-{target}**...")
            
            raw_info = get_carrier_info(target, CARRIER_TOKEN)
            st.session_state.scraped_rows.append(parse_carrier_data(target, raw_info))
            st.session_state.current_mc += 1
            time.sleep(max(0.1, delay_ms / 1000.0))
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
