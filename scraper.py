import requests
import pandas as pd
import os
import time
from datetime import datetime, timedelta
import streamlit as st
import streamlit.components.v1 as components
from supabase import create_client, Client

st.set_page_config(page_title="Carrier Automation Portal", layout="wide")

# --- SUPABASE & TOKEN CONFIGURATION ---
SUPABASE_URL = (
    os.environ.get("SUPABASE_URL") 
    or st.secrets.get("SUPABASE_URL", "") 
    or "https://vhudqthehrjttbcqluat.supabase.co"
)

SUPABASE_KEY = (
    os.environ.get("SUPABASE_KEY") 
    or st.secrets.get("SUPABASE_KEY", "") 
    or "sb_publishable_eHNwQ5RLe8oi1uZ7If3ODg_aZR66HJ7"
)

if not SUPABASE_URL or not SUPABASE_KEY:
    st.error("🔑 Database configuration missing! Please check your Supabase credentials.")
    st.stop()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Fetch Carrier Token from Environment or Secrets
CARRIER_TOKEN = os.environ.get("CARRIER_TOKEN") or st.secrets.get("CARRIER_TOKEN", "3243d1219423e4ea")

# --- BACKEND DATABASE UTILITIES ---
def log_activity(email, action, detail=""):
    try:
        supabase.table("activity_logs").insert({
            "email": email,
            "action": action,
            "detail": detail
        }).execute()
    except Exception:
        pass  

def get_system_config():
    """Fetches global config settings from database."""
    config = {"throttle_delay_ms": 250.00, "override_global_speed": False}
    try:
        res = supabase.table("system_config").select("*").execute()
        for row in res.data:
            if row["key"] == "throttle_delay_ms":
                config["throttle_delay_ms"] = float(row["value"])
            elif row["key"] == "override_global_speed":
                config["override_global_speed"] = row["value"].upper() == "TRUE"
    except Exception as e:
        print(f"Error fetching system config: {e}")
    return config

def update_global_config(delay_ms, override_bool):
    """Saves global settings to database."""
    try:
        supabase.table("system_config").upsert(
            {"key": "throttle_delay_ms", "value": f"{delay_ms:.4f}"},
            on_conflict="key"
        ).execute()
        supabase.table("system_config").upsert(
            {"key": "override_global_speed", "value": str(override_bool).upper()},
            on_conflict="key"
        ).execute()
        return True
    except Exception as e:
        st.error(f"Database error saving global speed config: {e}")
        return False

def get_user_settings(email):
    """Fetches custom speed and custom auto-lock duration assigned to user."""
    try:
        res = supabase.table("users").select("delay_ms, session_duration_hours").eq("email", email).execute()
        if res.data:
            delay = float(res.data[0].get("delay_ms", 250.00))
            duration = float(res.data[0].get("session_duration_hours", 3.0))
            return delay, duration
    except Exception as e:
        print(f"Error fetching user settings: {e}")
    return 250.00, 3.0

# --- CARRIERCHK API UTILITIES WITH ROBUST RETRIES ---
def get_carrier_info(mc_number, token, retries=6):
    url = "https://carrierchk.com/api/carrier"
    params = {
        "type": "mc",
        "value": str(mc_number).strip(),
        "token": token
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }

    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=(5.0, 10.0))
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict) and ("carrier" in data or "error" in data):
                    return data
            elif response.status_code == 429:
                time.sleep(2.0 * (attempt + 1))
                continue
            
            time.sleep(1.0 * (attempt + 1))
        except requests.exceptions.RequestException:
            time.sleep(1.5 * (attempt + 1))

    try:
        time.sleep(3.0)
        final_resp = requests.get(url, params=params, headers=headers, timeout=10.0)
        if final_resp.status_code == 200:
            return final_resp.json()
    except Exception:
        pass

    return "API_ERROR"

def parse_carrier_data(mc_number, raw_data):
    if raw_data == "API_ERROR":
        return {
            "MC Number": f"MC-{mc_number}",
            "Carrier Name": "⚠️ API THROTTLED (RETRY NEEDED)",
            "Operating Status": "⚠️ UNKNOWN",
            "Phone Number": "N/A",
            "Email Address": "N/A",
            "Location": "N/A"
        }

    if not raw_data or not isinstance(raw_data, dict) or "carrier" not in raw_data or not raw_data["carrier"]:
        return {
            "MC Number": f"MC-{mc_number}",
            "Carrier Name": "DOCKET NOT FOUND / DEAD NUMBER",
            "Operating Status": "❌ INVALID",
            "Phone Number": "N/A",
            "Email Address": "N/A",
            "Location": "N/A"
        }
    
    c = raw_data.get("carrier", {})
    
    status_code = str(c.get("status_code", "")).upper()
    allowed_to_operate = str(c.get("allowed_to_operate", "")).upper()
    common_auth = str(c.get("common_authority_status", "")).upper()
    contract_auth = str(c.get("contract_authority_status", "")).upper()
    
    is_active = (
        status_code == "A" 
        or allowed_to_operate == "Y" 
        or "ACTIVE" in common_auth 
        or "ACTIVE" in contract_auth
    )
    
    if is_active:
        status = "🟢 ACTIVE"
    elif status_code:
        status = f"🔴 INACTIVE ({status_code})"
    else:
        status = "🔴 INACTIVE"
    
    phone = c.get("phone") or c.get("cell_phone") or "N/A"
    
    email = c.get("email_address")
    if not email or str(email).strip() == "":
        email = "Not Listed"
    
    city = c.get("phy_city", "").strip()
    state = c.get("phy_state", "").strip()
    location = f"{city}, {state}".strip(", ") if city or state else "N/A"
    
    return {
        "MC Number": f"MC-{mc_number}",
        "Carrier Name": c.get("dba_name") or c.get("legal_name") or "N/A",
        "Operating Status": status,
        "Phone Number": phone,
        "Email Address": email,
        "Location": location
    }

# --- STATE INITIALIZATIONS ---
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "current_user" not in st.session_state:
    st.session_state.current_user = None
if "is_admin" not in st.session_state:
    st.session_state.is_admin = False
if "login_time" not in st.session_state:
    st.session_state.login_time = None
if "start_mc_log" not in st.session_state:
    st.session_state.start_mc_log = None
if "last_mc_log" not in st.session_state:
    st.session_state.last_mc_log = None

# Smart Caching Layers
if "last_db_check" not in st.session_state:
    st.session_state.last_db_check = 0.0
if "cached_delay_ms" not in st.session_state:
    st.session_state.cached_delay_ms = 250.0
if "cached_session_duration" not in st.session_state:
    st.session_state.cached_session_duration = 3.0
if "cached_speed_mode_string" not in st.session_state:
    st.session_state.cached_speed_mode_string = "👤 250.00 ms"

# --- AUTO-LOGOUT HELPER ---
def force_logout(reason="Session Auto-Expired"):
    if st.session_state.authenticated and st.session_state.current_user:
        if st.session_state.start_mc_log and st.session_state.last_mc_log:
            log_activity(
                st.session_state.current_user, 
                "search_batch", 
                f"Searched MC-{st.session_state.start_mc_log} to MC-{st.session_state.last_mc_log}"
            )
        log_activity(st.session_state.current_user, "logout", reason)
        st.session_state.authenticated = False
    st.session_state.current_user = None
    st.session_state.is_admin = False
    st.session_state.login_time = None
    st.session_state.running = False
    st.session_state.scraped_rows = []
    st.session_state.current_mc = ""
    st.session_state.start_mc_log = None
    st.session_state.last_mc_log = None
    st.session_state.last_db_check = 0.0

# --- LOGIN GATEWAY ---
if not st.session_state.authenticated:
    st.title("🔒 Security Access Required")
    st.write(
        "This engine is locked. Enter your assigned email and password to begin. "
        "To get access please contact **my176business@gmail.com** or WhatsApp at **+923097503520**"
    )
    
    col_l1, col_l2 = st.columns(2)
    with col_l1:
        email_input = st.text_input("Email Address:", placeholder="user@domain.com").strip().lower()
    with col_l2:
        password_input = st.text_input("Password:", type="password", placeholder="••••••••")
        
    if st.button("Verify & Unlock Engine", use_container_width=True):
        response = supabase.table("users").select("*").eq("email", email_input).execute()
        user_records = response.data
        
        if user_records and user_records[0]["password"] == password_input:
            st.session_state.scraped_rows = []
            st.session_state.current_mc = ""  
            st.session_state.running = False
            st.session_state.start_mc_log = None
            st.session_state.last_mc_log = None
            
            st.session_state.authenticated = True
            st.session_state.current_user = email_input
            st.session_state.is_admin = user_records[0].get("is_admin", False)
            st.session_state.login_time = time.time()
            st.session_state.last_db_check = 0.0  
            
            log_activity(email_input, "login", "Logged in successfully")
            st.success(f"Access Granted! Welcome, {email_input}.")
            st.rerun()
        else:
            st.error("Access denied: Invalid credentials.")
    st.stop()

# --- THROTTLED CONFIG RETRIEVAL ---
now = time.time()
if now - st.session_state.last_db_check > 10.0:
    sys_cfg = get_system_config()
    if sys_cfg["override_global_speed"]:
        st.session_state.cached_delay_ms = sys_cfg["throttle_delay_ms"]
        st.session_state.cached_speed_mode_string = f"🚨 Forced Global Override ({st.session_state.cached_delay_ms:.2f} ms)"
        _, st.session_state.cached_session_duration = get_user_settings(st.session_state.current_user)
    else:
        st.session_state.cached_delay_ms, st.session_state.cached_session_duration = get_user_settings(st.session_state.current_user)
        st.session_state.cached_speed_mode_string = f"👤 {st.session_state.cached_delay_ms:.2f} ms"
        st.session_state.last_db_check = now

current_delay_ms = st.session_state.cached_delay_ms
live_session_duration = st.session_state.cached_session_duration
speed_mode_string = st.session_state.cached_speed_mode_string

# --- AUTO-LOCK CHECK ---
if st.session_state.login_time:
    session_timeout_seconds = live_session_duration * 3600
    elapsed_time = time.time() - st.session_state.login_time
    if elapsed_time >= session_timeout_seconds:
        force_logout("Session Auto-Expired")
        st.warning("⏱️ Session Expired: Your custom session has ended. Please log in again.")
        st.rerun()

# --- SIDEBAR USER CARD & TIMER ---
st.sidebar.markdown(f"### 👤 Logged In As:")
st.sidebar.info(st.session_state.current_user)

session_timeout_seconds = live_session_duration * 3600
elapsed_time = time.time() - st.session_state.login_time
remaining_seconds = max(0, int(session_timeout_seconds - elapsed_time))

st.sidebar.markdown(f"### ⏱️ Session Security Lockout")
timer_html = f"""
<div style="font-family: monospace; font-size: 16px; font-weight: bold; color: #ff4b4b; background-color: #0e1117; padding: 10px; border-radius: 5px; text-align: center; border: 1px solid #30363d; margin-bottom: 10px;">
    Auto-Locks In: <span id="clock">--h --m --s</span>
</div>
<script>
    let remaining = {remaining_seconds};
    const clockSpan = document.getElementById('clock');
    
    function updateClock() {{
        if (remaining <= 0) {{
            clockSpan.textContent = "EXPIRED";
            window.parent.location.reload();
            return;
        }}
        
        let hours = Math.floor(remaining / 3600);
        let minutes = Math.floor((remaining % 3600) / 60);
        let seconds = remaining % 60;
        
        hours = hours < 10 ? "0" + hours : hours;
        minutes = minutes < 10 ? "0" + minutes : minutes;
        seconds = seconds < 10 ? "0" + seconds : seconds;
        
        clockSpan.textContent = hours + "h " + minutes + "m " + seconds + "s";
        remaining--;
    }}
    
    updateClock();
    setInterval(updateClock, 1000);
</script>
"""
with st.sidebar:
    components.html(timer_html, height=65)

if st.sidebar.button("🔓 Manual Log Out", use_container_width=True):
    force_logout("Manual Logout")
    st.rerun()

# --- ADMIN PANEL CHECK ---
show_admin_panel = False
if st.session_state.is_admin:
    st.sidebar.markdown("---")
    show_admin_panel = st.sidebar.checkbox("🛡️ Open Admin Dashboard", value=False)

# --- ADMIN PANEL RENDERING ---
if show_admin_panel and st.session_state.is_admin:
    st.title("🛡️ Super Admin Control Dashboard")
    adm_tab1, adm_tab2, adm_tab3 = st.tabs(["👥 User Account Management", "📊 30-Day Activity logs", "⚙️ System Configuration"])
    
    with adm_tab1:
        st.subheader("Add Single User Account")
        col_add1, col_add2, col_add3 = st.columns(3)
        with col_add1:
            new_email = st.text_input("New User Email:", placeholder="driver@company.com", key="n_email").strip().lower()
        with col_add2:
            new_pass = st.text_input("Set Password:", placeholder="StrongPass123!", key="n_pass")
        with col_add3:
            new_role = st.selectbox("Role Status:", ["Standard User", "Super Admin"], key="n_role")
            
        st.markdown("**User Settings:**")
        col_add4, col_add5, col_add6, col_add7 = st.columns(4)
        with col_add4:
            new_delay = st.number_input("Custom Speed (ms):", min_value=0.01, max_value=2000.0, value=250.0, step=0.01, format="%.2f", key="n_delay")
        with col_add5:
            new_hrs = st.number_input("Session Hours:", min_value=0, max_value=24, value=3, step=1, key="n_hrs")
        with col_add6:
            new_mins = st.number_input("Session Minutes:", min_value=0, max_value=59, value=0, step=1, key="n_mins")
        with col_add7:
            new_secs = st.number_input("Session Seconds:", min_value=0, max_value=59, value=0, step=1, key="n_secs")
            
        if st.button("➕ Register Single User Account", use_container_width=True):
            if new_email and new_pass:
                try:
                    # Check if user already exists
                    existing_user = supabase.table("users").select("email").eq("email", new_email).execute().data
                    if existing_user:
                        st.error(f"User with email '{new_email}' already exists!")
                    else:
                        role_bool = True if new_role == "Super Admin" else False
                        total_hours_decimal = float(new_hrs) + (float(new_mins) / 60.0) + (float(new_secs) / 3600.0)
                        
                        if total_hours_decimal <= 0.0:
                            st.error("Session lockout duration must be greater than 0 seconds.")
                        else:
                            # Insert single user record into Supabase
                            supabase.table("users").insert({
                                "email": new_email,
                                "password": new_pass,
                                "is_admin": role_bool,
                                "delay_ms": float(new_delay),
                                "session_duration_hours": total_hours_decimal
                            }).execute()
                            
                            log_activity(st.session_state.current_user, "add_user", f"Added user: {new_email}")
                            st.success(f"Successfully registered single account for {new_email}!")
                            time.sleep(1)
                            st.rerun()
                except Exception as e:
                    st.error(f"Error registering user: {str(e)}")
            else:
                st.warning("Email and Password fields cannot be empty.")
                
        st.markdown("---")
        st.subheader("Existing Authorized Users")
        user_list = supabase.table("users").select("*").execute().data
        if user_list:
            user_df = pd.DataFrame(user_list)
            
            def convert_hours_to_hms_str(h_decimal):
                td = timedelta(hours=float(h_decimal))
                tot_sec = int(td.total_seconds())
                h = tot_sec // 3600
                m = (tot_sec % 3600) // 60
                s = tot_sec % 60
                return f"{h}h {m}m {s}s"
            
            user_df["Readable Timeout"] = user_df["session_duration_hours"].apply(convert_hours_to_hms_str)
            display_cols = ["email", "is_admin", "delay_ms", "Readable Timeout"]
            st.dataframe(user_df[[c for c in display_cols if c in user_df.columns]], use_container_width=True)
            
            st.subheader("⚙️ Edit User Limits & Safety Guidelines")
            col_mod1, col_mod2 = st.columns([2, 1])
            with col_mod1:
                target_mod_email = st.selectbox("Choose account to modify parameters:", [u["email"] for u in user_list])
                
            current_user_delay = next((u.get("delay_ms", 250.0) for u in user_list if u["email"] == target_mod_email), 250.0)
            current_user_lock = next((u.get("session_duration_hours", 3.0) for u in user_list if u["email"] == target_mod_email), 3.0)
            
            tot_sec_cur = int(float(current_user_lock) * 3600)
            cur_hrs = tot_sec_cur // 3600
            cur_mins = (tot_sec_cur % 3600) // 60
            cur_secs = tot_sec_cur % 60
            
            with col_mod2:
                new_user_delay = st.number_input(
                    "Update Speed Limit (ms):", 
                    min_value=0.01, 
                    max_value=2000.0, 
                    value=float(current_user_delay), 
                    step=0.1, 
                    format="%.2f",
                    key="edit_speed_user"
                )
                
            st.markdown("**Update Timeout Value:**")
            col_u1, col_u2, col_u3 = st.columns(3)
            with col_u1:
                edit_hrs = st.number_input("Hours:", min_value=0, max_value=24, value=cur_hrs, step=1, key="e_hrs")
            with col_u2:
                edit_mins = st.number_input("Minutes:", min_value=0, max_value=59, value=cur_mins, step=1, key="e_mins")
            with col_u3:
                edit_secs = st.number_input("Seconds:", min_value=0, max_value=59, value=cur_secs, step=1, key="e_secs")
                
            if st.button("⚡ Apply Updates for Selected Account", use_container_width=True):
                try:
                    total_edit_hours_decimal = float(edit_hrs) + (float(edit_mins) / 60.0) + (float(edit_secs) / 3600.0)
                    if total_edit_hours_decimal <= 0.0:
                        st.error("Session limit must be greater than 0 seconds.")
                    else:
                        supabase.table("users").update({
                            "delay_ms": float(new_user_delay),
                            "session_duration_hours": total_edit_hours_decimal
                        }).eq("email", target_mod_email).execute()
                        
                        st.success(f"Successfully configured {target_mod_email}!")
                        st.session_state.last_db_check = 0.0
                        time.sleep(1)
                        st.rerun()
                except Exception as e:
                    st.error(f"Failed to update database: {e}")
                    
            st.markdown("---")
            st.subheader("Terminate User Account")
            del_email = st.selectbox("Select account to delete:", [u["email"] for u in user_list if u["email"] != st.session_state.current_user])
            if st.button("🗑️ Delete Selected Account", type="primary"):
                supabase.table("users").delete().eq("email", del_email).execute()
                st.success(f"Terminated access for {del_email}.")
                time.sleep(1)
                st.rerun()

    with adm_tab2:
        st.subheader("User Activity Analytics (Last 30 Days)")
        thirty_days_ago = (datetime.now() - timedelta(days=30)).isoformat()
        logs = supabase.table("activity_logs").select("*").gte("created_at", thirty_days_ago).order("created_at", desc=True).execute().data
        
        if logs:
            logs_df = pd.DataFrame(logs)
            logs_df["Time & Date"] = pd.to_datetime(logs_df["created_at"]).dt.strftime('%Y-%m-%d %I:%M:%S %p')
            
            col_kpi1, col_kpi2, col_kpi3 = st.columns(3)
            col_kpi1.metric("Total Login/Logout Actions", len(logs_df[logs_df["action"].isin(["login", "logout"])]))
            col_kpi2.metric("Total Batch Search Events", len(logs_df[logs_df["action"] == "search_batch"]))
            col_kpi3.metric("Active Working Users", logs_df["email"].nunique())
            
            st.subheader("Leaderboard: Active Team Members")
            leaderboard = logs_df[logs_df["action"] == "search_batch"].groupby("email").size().reset_index(name="Sequences Run")
            st.dataframe(leaderboard.sort_values(by="Sequences Run", ascending=False), use_container_width=True)
            
            st.subheader("Raw History Streams")
            clean_logs_show = logs_df[["Time & Date", "email", "action", "detail"]].rename(columns={
                "email": "Email Address",
                "action": "Action Taken",
                "detail": "Action Details"
            })
            st.dataframe(clean_logs_show, use_container_width=True)
        else:
            st.info("No system activity logs found in the last 30 days.")

    with adm_tab3:
        st.subheader("Global Scraper Speed Configuration")
        sys_config = get_system_config()
        
        override_switch = st.checkbox(
            "⚠️ Activate Global Speed Override (Ignores individual user delays)", 
            value=sys_config["override_global_speed"]
        )
        
        global_speed_slider = st.number_input(
            "Default Global Speed Limit (ms):", 
            min_value=0.01, 
            max_value=2000.0, 
            value=float(sys_config["throttle_delay_ms"]), 
            step=0.1, 
            format="%.2f"
        )
        
        st.info(f"Equivalent Override Value: **{global_speed_slider / 1000.0:.5f} seconds** per request.")
        
        if st.button("💾 Save Global Settings", use_container_width=True):
            if update_global_config(float(global_speed_slider), override_switch):
                st.success("Successfully updated system configurations!")
                st.session_state.last_db_check = 0.0
                time.sleep(1)
                st.rerun()

# --- MAIN HARVESTER ENGINE ---
if not show_admin_panel:
    st.title("🚚 Automated Carrier Harvester")
    st.write("Sequential tracking engine powered by CarrierChk. Enter a starting MC number to run live validation cycles.")
    
    st.sidebar.header("🛡️ API Connection Status")
    if CARRIER_TOKEN:
        st.sidebar.success("CarrierChk API Active")
    else:
        st.sidebar.warning("Carrier Token Missing")

    if "running" not in st.session_state:
        st.session_state.running = False
    if "current_mc" not in st.session_state or st.session_state.current_mc == "":
        st.session_state.current_mc = ""  
    if "scraped_rows" not in st.session_state:
        st.session_state.scraped_rows = []

    col_in1, col_in2 = st.columns(2)
    with col_in1:
        if st.session_state.current_mc == "":
            raw_mc_input = st.text_input("Enter Starting MC Number to Begin:", value="", placeholder="e.g., 1066434")
            if raw_mc_input.isdigit():
                st.session_state.current_mc = int(raw_mc_input)
        else:
            st.session_state.current_mc = st.number_input("Set Starting MC Number:", min_value=1, value=int(st.session_state.current_mc), step=1)
            
    with col_in2:
        st.metric("Enforced Speed Limit for Your Session", speed_mode_string)

    col_btn1, col_btn2, col_btn3 = st.columns(3)
    if col_btn1.button("🚀 Start Sequence", use_container_width=True):
        if st.session_state.current_mc == "":
            st.error("Please enter a starting MC Number before running the automation sequence.")
        else:
            st.session_state.running = True
            st.session_state.start_mc_log = int(st.session_state.current_mc)
            st.session_state.last_mc_log = int(st.session_state.current_mc)
            st.rerun()

    if col_btn2.button("🛑 STOP Sequence", use_container_width=True):
        st.session_state.running = False
        if st.session_state.start_mc_log is not None and st.session_state.last_mc_log is not None:
            log_activity(
                st.session_state.current_user, 
                "search_batch", 
                f"Searched MC-{st.session_state.start_mc_log} to MC-{st.session_state.last_mc_log}"
            )
            st.session_state.start_mc_log = None
            st.session_state.last_mc_log = None
            
        st.success(f"Automation paused cleanly at MC-{st.session_state.current_mc}")

    if col_btn3.button("🗑️ Clear Collected Data", use_container_width=True):
        st.session_state.scraped_rows = []
        st.success("Internal data sheet cleared.")
        st.rerun()

    # --- AUTOMATION ENGINE LOOP ---
    if st.session_state.running and st.session_state.current_mc != "":
        target_mc = str(st.session_state.current_mc)
        
        status_box = st.empty()
        status_box.info(f"Processing target line item: **MC-{target_mc}**...")
        
        st.session_state.last_mc_log = int(st.session_state.current_mc)
        
        raw_info = get_carrier_info(target_mc, CARRIER_TOKEN)
        parsed_row = parse_carrier_data(target_mc, raw_info)
        
        st.session_state.scraped_rows.append(parsed_row)
        
        st.session_state.current_mc += 1
        safety_delay_seconds = max(0.35, current_delay_ms / 1000.0)
        time.sleep(safety_delay_seconds)
        st.rerun()

    # --- TABBED DISPLAY & EXPORT ---
    st.markdown("---")
    if st.session_state.scraped_rows:
        base_df = pd.DataFrame(st.session_state.scraped_rows)
        tab1, tab2, tab3 = st.tabs(["📋 Complete Master Log", "🎯 Verified Leads (Full Info)", "📧 Raw Email List"])
        
        with tab1:
            st.subheader("Master History Sheet")
            st.dataframe(base_df, use_container_width=True)
            
            master_csv = base_df.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="📥 Export Master Sheet to CSV",
                data=master_csv,
                file_name="Master_MC_Harvest_Log.csv",
                mime="text/csv",
                use_container_width=True,
                key="master_download"
            )
            
        with tab2:
            st.subheader("Clean Target Pitch Sheet")
            leads_df = base_df[
                (base_df["Email Address"] != "N/A") & 
                (base_df["Email Address"] != "Not Listed") & 
                (base_df["Email Address"].str.contains("@", na=False))
            ]
            
            if not leads_df.empty:
                st.success(f"Filtered out {len(leads_df)} verified carrier targets with full records!")
                st.dataframe(leads_df, use_container_width=True)
                
                leads_csv = leads_df.to_csv(index=False).encode('utf-8')
                st.download_button(
                    label="📥 Export Clean Email Pitch Sheet to CSV",
                    data=leads_csv,
                    file_name="Verified_Carrier_Emails.csv",
                    mime="text/csv",
                    use_container_width=True,
                    key="leads_download"
                )
            else:
                st.info("No valid email addresses identified in this sequence run yet.")

        with tab3:
            st.subheader("Isolated Email Blast Column")
            
            valid_emails = base_df[
                (base_df["Email Address"] != "N/A") & 
                (base_df["Email Address"] != "Not Listed") & 
                (base_df["Email Address"].str.contains("@", na=False))
            ]["Email Address"].drop_duplicates()
            
            if not valid_emails.empty:
                just_emails_df = pd.DataFrame({"Email Address": valid_emails})
                
                st.success(f"Found {len(just_emails_df)} unique emails for direct copy-pasting!")
                st.dataframe(just_emails_df, use_container_width=True)
                
                email_text = "\n".join(just_emails_df["Email Address"].tolist())
                st.text_area("Copy Raw Emails:", value=email_text, height=150)
                
                raw_emails_csv = just_emails_df.to_csv(index=False).encode('utf-8')
                st.download_button(
                    label="📥 Export Isolated Email List to CSV",
                    data=raw_emails_csv,
                    file_name="Clean_Mailing_List.csv",
                    mime="text/csv",
                    use_container_width=True,
                    key="raw_emails_download"
                )
            else:
                st.info("No valid email leads found to populate the mailing column yet.")
    else:
        st.info("No data rows collected in this run yet. Click 'Start Sequence' to begin harvesting.")
