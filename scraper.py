import os
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
            print(f"[-] Request failed ({response.status_code}): {response.text}")
            return None
    except Exception as e:
        print(f"[-] Error: {e}")
        return None

if __name__ == "__main__":
    # Safely retrieve token from Streamlit secrets or fall back to default
    default_token = "3243d1219423e4ea"
    try:
        token = st.secrets.get("CARRIER_TOKEN", default_token)
    except Exception:
        token = default_token

    mc_to_test = "1066434"
    print(f"Fetching carrier details for MC #{mc_to_test}...")
    
    data = get_carrier_info(mc_to_test, token)
    if data:
        print("[+] Carrier Data Received:")
        print(data)
    else:
        print("[-] Failed to retrieve carrier details.")
