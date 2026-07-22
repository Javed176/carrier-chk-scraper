import requests
from bs4 import BeautifulSoup

def lookup_carrier(number, search_type="mc"):
    """
    Looks up carrier information on carrierchk.com
    :param number: USDOT or MC Number (e.g., '1800000' or '4535979')
    :param search_type: 'mc' or 'usdot'
    """
    # Format prefix according to carrierchk's direct URL structure
    clean_num = str(number).strip()
    if search_type.lower() == "mc":
        identifier = f"MC{clean_num}" if not clean_num.upper().startswith("MC") else clean_num.upper()
    else:
        identifier = f"USDOT{clean_num}" if not clean_num.upper().startswith("USDOT") else clean_num.upper()

    url = f"https://carrierchk.com/carrier/{identifier}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    }

    print(f"Fetching carrier details from: {url}")

    try:
        response = requests.get(url, headers=headers, timeout=12)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"[-] Error fetching data: {e}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    data = {}

    # Extract Company Title
    heading = soup.find("h1") or soup.find("h2")
    if heading:
        data["company_name"] = heading.get_text(strip=True)

    # Extract Status / Authority Badge
    badge = soup.find(text=lambda t: t and "Authority" in t)
    if badge and badge.parent:
        data["authority_status"] = badge.parent.get_text(strip=True)

    # Scrape key-value details from page blocks
    for element in soup.find_all(["div", "p", "span"]):
        text = element.get_text(strip=True)
        if "USDOT" in text and "usdot" not in data:
            nxt = element.find_next_sibling()
            if nxt:
                data["usdot"] = nxt.get_text(strip=True)
        elif "MC DOCKET" in text and "mc_docket" not in data:
            nxt = element.find_next_sibling()
            if nxt:
                data["mc_docket"] = nxt.get_text(strip=True)

    return data

if __name__ == "__main__":
    # Test MC lookup
    mc_number = "1800000"
    result = lookup_carrier(mc_number, search_type="mc")

    if result:
        print("\n--- Carrier Results ---")
        for key, value in result.items():
            print(f"{key.replace('_', ' ').title()}: {value}")
    else:
        print("No details found or request failed.")
