import requests
from bs4 import BeautifulSoup

def lookup_carrier(number, search_type="mc"):
    """
    Looks up carrier details on carrierchk.com
    :param number: USDOT or MC Number (e.g., '1800000' or '4535979')
    :param search_type: 'mc' or 'usdot'
    """
    clean_num = str(number).strip()
    
    # CarrierCheck query format
    url = f"https://carrierchk.com/search?q={clean_num}"
    if search_type.lower() == "usdot":
        url += "&type=usdot"
    elif search_type.lower() == "mc":
        url += "&type=mc"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    print(f"Fetching carrier details from: {url}")

    session = requests.Session()
    try:
        response = session.get(url, headers=headers, timeout=12)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"[-] Error fetching data: {e}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    data = {}

    # Parse Headings & Main Info
    heading = soup.find(["h1", "h2", "h3"])
    if heading:
        data["company_name"] = heading.get_text(strip=True)

    # Extract all text elements / Key-Value cards
    text_nodes = [tag.get_text(strip=True) for tag in soup.find_all(["div", "span", "p", "td"]) if tag.get_text(strip=True)]
    
    for i, text in enumerate(text_nodes):
        if "USDOT" in text.upper() and "usdot" not in data and i + 1 < len(text_nodes):
            data["usdot"] = text_nodes[i + 1]
        elif "MC" in text.upper() and "DOCKET" in text.upper() and "mc_docket" not in data and i + 1 < len(text_nodes):
            data["mc_docket"] = text_nodes[i + 1]
        elif "STATUS" in text.upper() and "status" not in data and i + 1 < len(text_nodes):
            data["status"] = text_nodes[i + 1]

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
