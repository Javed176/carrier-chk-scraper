import requests
import streamlit as st

def get_carrier_info(mc_number, token):
    url = "https://carrierchk.com/api/carrier"
    params = {
        "type": "mc",
        "value": mc_number,
        "token": token
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json"
    }
    
    try:
        response = requests.get(url, params=params, headers=headers)
        if response.status_code == 200:
            return response.json()
        else:
            st.error(f"Request failed ({response.status_code}): {response.text}")
            return None
    except Exception as e:
        st.error(f"Error: {e}")
        return None

# --- Streamlit UI ---
st.set_page_config(page_title="Carrier Checker", page_icon="🚚")

st.title("🚚 Carrier Checker")

# Retrieve token from secrets or fallback
default_token = "3243d1219423e4ea"
try:
    token = st.secrets.get("CARRIER_TOKEN", default_token)
except Exception:
    token = default_token

# User Input Form
mc_number = st.text_input("Enter MC Number:", value="1066434")

if st.button("Fetch Carrier Details"):
    if mc_number:
        with st.spinner("Fetching details..."):
            data = get_carrier_info(mc_number, token)
            
        if data:
            st.success("Carrier Details Retrieved!")
            st.json(data)
        else:
            st.warning("Failed to retrieve carrier details.")
    else:
        st.error("Please enter a valid MC number.")
